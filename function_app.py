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
# 1. CONFIGURACIÓN (Variables de Entorno)
# ==========================================
EMAIL_USUARIO = os.environ.get("EMAIL_USUARIO")
PASSWORD_APP = os.environ.get("PASSWORD_APP")

SQL_CONN_STR = os.environ.get(
    "SQL_CONN_STR",
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=ngiadfserver1.database.windows.net;"
    "DATABASE=db_bumble_project;"
    "Authentication=ActiveDirectoryMsi;"
    "Encrypt=yes;"
    "TrustServerCertificate=no;"
)

app = func.FunctionApp()

@app.route(route="procesar_bumble", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "GET"])
def procesar_reporte_bumble(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Iniciando conexión a Gmail...")
    
    if not EMAIL_USUARIO or not PASSWORD_APP:
        logging.error("Faltan las credenciales de correo en las variables de entorno.")
        return func.HttpResponse("Error: Variables de entorno no configuradas.", status_code=500)

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_USUARIO, PASSWORD_APP)
        mail.select("inbox")

        # ==========================================
        # 2. BUSCAR LOS 2 CORREOS MÁS RECIENTES
        # ==========================================
        status, data = mail.search(None, '(FROM "noreplydata@bumble.com")')
        email_ids = data[0].split()

        if len(email_ids) < 2:
            logging.error("No se encontraron suficientes correos de Bumble en la bandeja.")
            return func.HttpResponse("No se encontraron suficientes correos.", status_code=404)

        id_correo_2 = email_ids[-1]  # El más reciente
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

        # --- LOGS DE DEPURACIÓN ---
        logging.info(f"--- CONTENIDO EXTRAÍDO DE LOS CORREOS ---\n{texto_combinado}\n-----------------------------------")

        # ==========================================
        # 3. EXTRAER URL DEL ZIP Y CONTRASEÑA
        # ==========================================
        match_url = re.search(r'(https://static-eu\.bumble\.com/data/\S+)', texto_combinado)
        match_pwd = re.search(r'contraseña única\.\s*\r?\n\r?\n([A-Za-z0-9]{16})', texto_combinado)

        if not match_url or not match_pwd:
            logging.error("No se pudo extraer la URL del reporte o la contraseña.")
            return func.HttpResponse("Error al extraer URL o contraseña del correo.", status_code=400)

        url_zip = match_url.group(1)
        password_zip = match_pwd.group(1)
        logging.info("URL y contraseña encontradas exitosamente.")

        # ==========================================
        # 4. DESCARGAR EL ARCHIVO ZIP
        # ==========================================
        respuesta = requests.get(url_zip)
        if respuesta.status_code != 200:
            logging.error(f"Error al descargar el ZIP. HTTP: {respuesta.status_code}")
            return func.HttpResponse("Error al descargar archivo ZIP de Bumble.", status_code=502)

        # ==========================================
        # 5. DESCOMPRIMIR EN MEMORIA
        # ==========================================
        pdf_bytes = None
        with zipfile.ZipFile(io.BytesIO(respuesta.content)) as z:
            for filename in z.namelist():
                if filename.endswith(".pdf") or "report.pdf" in filename.lower():
                    pdf_bytes = z.read(filename, pwd=bytes(password_zip, "utf-8"))
                    break

        if not pdf_bytes:
            logging.error("No se pudo extraer el PDF del archivo ZIP.")
            return func.HttpResponse("Error al descomprimir el PDF.", status_code=400)

        # ==========================================
        # 6. EXTRAER MÉTRICAS CON PDFPLUMBER
        # ==========================================
        full_text = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                full_text += (page.extract_text() or "") + "\n"

        out_yes = re.search(r'Outgoing\s+"yes"\s+(\d+)', full_text, re.IGNORECASE)
        out_no  = re.search(r'Outgoing\s+"no"\s+(\d+)', full_text, re.IGNORECASE)
        inc_yes = re.search(r'Incoming\s+"yes"\s+(\d+)', full_text, re.IGNORECASE)
        inc_no  = re.search(r'Incoming\s+"no"\s+(\d+)', full_text, re.IGNORECASE)

        val_out_yes = int(out_yes.group(1)) if out_yes else 0
        val_out_no  = int(out_no.group(1))  if out_no  else 0
        val_inc_yes = int(inc_yes.group(1)) if inc_yes else 0
        val_inc_no  = int(inc_no.group(1))  if inc_no  else 0

        match_fecha = re.search(r'collected at:\s*(\d{4}-\d{2}-\d{2})', full_text, re.IGNORECASE)
        fecha_reporte = match_fecha.group(1) if match_fecha else str(date.today())

        logging.info(f"Métricas ({fecha_reporte}): OutYes={val_out_yes}, OutNo={val_out_no}")

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

        logging.info("¡Datos guardados con éxito en Azure SQL!")
        return func.HttpResponse("Proceso completado exitosamente", status_code=200)

    except Exception as e:
        logging.error(f"Error no controlado durante la ejecución: {str(e)}")
        return func.HttpResponse(f"Error interno: {str(e)}", status_code=500)