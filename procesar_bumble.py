import imaplib
import email
import zipfile
import re
import os
import io
import logging
import pdfplumber
import requests
import pyodbc
import azure.functions as func
from datetime import date

# ==========================================
# 1. CONFIGURACIÓN (Usa Variables de Entorno en Azure)
# ==========================================
# ==========================================

EMAIL_USUARIO = os.environ.get("EMAIL_USUARIO")
PASSWORD_APP = os.environ.get("PASSWORD_APP")

# Cadena de conexión a Azure SQL
SQL_CONN_STR = os.environ.get(
    "SQL_CONN_STR",
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=ngiadfserver1.database.windows.net;"
    "DATABASE=db_bumble_project;"
    "Authentication=ActiveDirectoryMsi;"
    "Encrypt=yes;"
    "TrustServerCertificate=no;"
)
def procesar_reporte_bumble():
    logging.info("Iniciando conexión a Gmail...")
    
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(EMAIL_USUARIO, PASSWORD_APP)
    mail.select("inbox")

    # ==========================================
    # 2. BUSCAR LOS 2 CORREOS MÁS RECIENTES DE BUMBLE
    # ==========================================
    status, data = mail.search(None, '(FROM "noreplydata@bumble.com")')
    email_ids = data[0].split()

    if len(email_ids) < 2:
        logging.error("No se encontraron suficientes correos de Bumble en la bandeja.")
        return

    # Tomamos los últimos 2 correos recibidos
    id_correo_2 = email_ids[-1]  # El más reciente (suele ser la contraseña o el link)
    id_correo_1 = email_ids[-2]  # El penúltimo

    def obtener_cuerpo_correo(email_id):
        _, data = mail.fetch(email_id, '(RFC822)')
        msg = email.message_from_bytes(data[0][1])
        cuerpo = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    cuerpo += part.get_payload(decode=True).decode(errors="ignore")
        else:
            cuerpo = msg.get_payload(decode=True).decode(errors="ignore")
        return cuerpo

    texto_1 = obtener_cuerpo_correo(id_correo_1)
    texto_2 = obtener_cuerpo_correo(id_correo_2)
    texto_combinado = texto_1 + "\n" + texto_2

    # ==========================================
    # 3. EXTRAER URL DEL ZIP Y CONTRASEÑA
    # ==========================================
    # Buscar el enlace de descarga que empiece con https://static-eu.bumble.com/...
    match_url = re.search(r'(https://static-eu\.bumble\.com/data/\S+)', texto_combinado)
    
    # Buscar la contraseña: En el correo llega después de "su contraseña única.\n\n"
    # Es un bloque de 16 caracteres alfanuméricos en su propia línea
    match_pwd = re.search(r'contraseña única\.\s*\r?\n\r?\n([A-Za-z0-9]{16})', texto_combinado)

    if not match_url or not match_pwd:
        logging.error("No se pudo extraer la URL del reporte o la contraseña de los correos.")
        return

    url_zip = match_url.group(1)
    password_zip = match_pwd.group(1)
    logging.info(f"URL de descarga y contraseña encontradas exitosamente.")

    # ==========================================
    # 4. DESCARGAR EL ARCHIVO ZIP DESDE LA URL
    # ==========================================
    logging.info("Descargando el archivo .zip desde los servidores de Bumble...")
    respuesta = requests.get(url_zip)
    if respuesta.status_code != 200:
        logging.error(f"Error al descargar el ZIP. Código HTTP: {respuesta.status_code}")
        return

    # ==========================================
    # 5. DESCOMPRIMIR REPORT.PDF EN MEMORIA
    # ==========================================
    pdf_bytes = None
    with zipfile.ZipFile(io.BytesIO(respuesta.content)) as z:
        for filename in z.namelist():
            if filename.endswith(".pdf") or "report.pdf" in filename.lower():
                pdf_bytes = z.read(filename, pwd=bytes(password_zip, "utf-8"))
                break

    if not pdf_bytes:
        logging.error("No se pudo extraer el PDF del archivo ZIP.")
        return

    # ==========================================
    # 6. EXTRAER MÉTRICAS CON PDFPLUMBER
    # ==========================================
    logging.info("Extrayendo métricas del PDF...")
    full_text = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

    # Extracción por Regex según el formato de tu captura: Outgoing "yes" 190
    out_yes = re.search(r'Outgoing\s+"yes"\s+(\d+)', full_text, re.IGNORECASE)
    out_no  = re.search(r'Outgoing\s+"no"\s+(\d+)', full_text, re.IGNORECASE)
    inc_yes = re.search(r'Incoming\s+"yes"\s+(\d+)', full_text, re.IGNORECASE)
    inc_no  = re.search(r'Incoming\s+"no"\s+(\d+)', full_text, re.IGNORECASE)

    val_out_yes = int(out_yes.group(1)) if out_yes else 0
    val_out_no  = int(out_no.group(1))  if out_no  else 0
    val_inc_yes = int(inc_yes.group(1)) if inc_yes else 0
    val_inc_no  = int(inc_no.group(1))  if inc_no  else 0

    # Intentar extraer la fecha exacta del encabezado: (collected at: 2026-07-29 ...)
    match_fecha = re.search(r'collected at:\s*(\d{4}-\d{2}-\d{2})', full_text, re.IGNORECASE)
    if match_fecha:
        fecha_reporte = match_fecha.group(1)
    else:
        fecha_reporte = str(date.today())

    logging.info(f"Métricas ({fecha_reporte}): OutYes={val_out_yes}, OutNo={val_out_no}, IncYes={val_inc_yes}, IncNo={val_inc_no}")

    # ==========================================
    # 7. GUARDAR EN AZURE SQL DATABASE
    # ==========================================
    query_upsert = """
    IF EXISTS (SELECT 1 FROM MetricasSwiping WHERE Fecha = ?)
        UPDATE MetricasSwiping 
        SET OutgoingYes = ?, OutgoingNo = ?, IncomingYes = ?, IncomingNo = ?
        WHERE Fecha = ?
    ELSE
        INSERT INTO MetricasSwiping (Fecha, OutgoingYes, OutgoingNo, IncomingYes, IncomingNo)
        VALUES (?, ?, ?, ?, ?)
    """

    with pyodbc.connect(SQL_CONN_STR) as conn:
        cursor = conn.cursor()
        cursor.execute(
            query_upsert,
            fecha_reporte, val_out_yes, val_out_no, val_inc_yes, val_inc_no, fecha_reporte,
            fecha_reporte, val_out_yes, val_out_no, val_inc_yes, val_inc_no
        )
        conn.commit()

    logging.info("¡Datos guardados con éxito en Azure SQL Database!")

if __name__ == "__main__":
    # Configuración de logs al probar en consola local
    logging.basicConfig(level=logging.INFO)
    procesar_reporte_bumble()