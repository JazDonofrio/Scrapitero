#!/usr/bin/env python3
"""
Script de Extracción Catastral Secuencial con Playwright

Autor: Ingeniero de Software Senior experto en Web Scraping y Procesamiento de Datos Masivos
Descripción: Realiza una extracción secuencial automatizada (fuerza bruta de IDs) en un
formulario público de catastro inmobiliario sin autenticación, formateando IDs a 15 dígitos,
manejando resiliencia, CAPTCHAs, rate limiting y guardando incrementalmente en CSV.
"""

import asyncio
import csv
import os
import random
import sys
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ==============================================================================
# CONFIGURACIÓN GENERAL Y VARIABLES DE ENTORNO
# ==============================================================================

# Cambiar a False para ver el navegador en modo visual durante la depuración
HEADLESS = True

# User-Agent residencial realista para evitar bloqueos por firmas automatizadas
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# URL base del formulario público de catastro
BASE_URL = "https://vg.abaco.com.br/eagata/servlet/hwloginusuario?55"

# Rango numérico de IDs a consultar
START_ID = 36100
END_ID = 36200

# Carpeta destino para descargas de PDF
PDF_DIR = "pdf_downloads"

# Parámetros de simulación humana (Stealth Mode)
# Rango de retraso aleatorio (en segundos) después de cada consulta exitosa
MIN_DELAY_SECS = 45
MAX_DELAY_SECS = 150

# Frecuencia de pausas largas (simulación de descansos del operador)
PAUSA_CADA_N_DESCARGAS = 12
PAUSA_MIN_MINUTOS = 5
PAUSA_MAX_MINUTOS = 15

# Archivo de salida CSV de registro
OUTPUT_CSV = "resultado_catastro_registro.csv"
CSV_HEADERS = ["Inscripción", "Tipo de Inmueble", "Logradouro", "Bairro", "Unidade", "CEP"]

# --- SELECTORES DEL DOM (Modificar para adaptar al portal real) ---
# Campo de texto donde se ingresa la identificación catastral
SELECTOR_INPUT_ID = "#vCONTRIBUINTEINSCRICAO"

# Botón para enviar/consultar el formulario
SELECTOR_BTN_CONSULTAR = "input[name='BTNCONSULTAR']"

# Selector que indica que el reporte de resultados se cargó correctamente
SELECTOR_RESULT_CONTAINER = ".boletim-cadastro, #TABLE_RESULTADOS, table.report-table"

# Selector para detectar mensajes de error ("inmueble inexistente")
SELECTOR_ERROR_NOT_FOUND = "text=Imóvel não encontrado, text=Inscrição não cadastrada, .error-message"

# Selectores para identificar desafíos de CAPTCHA
SELECTOR_CAPTCHA = "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], div#cf-turnstile-iframe, text=Verifique que es humano, .g-recaptcha"


# ==============================================================================
# FUNCIONES AUXILIARES Y LÓGICA DE EXTRACCIÓN
# ==============================================================================

def guardar_en_csv(registro: dict):
    """
    Guarda incrementalmente un registro válido en el archivo CSV.
    Utiliza codificación 'utf-8-sig' para preservar la compatibilidad con acentos
    y caracteres especiales en portugués al abrir el archivo en Excel.
    """
    archivo_existe = os.path.exists(OUTPUT_CSV)
    
    # Abrir en modo 'append' (a) para guardar fila por fila de forma segura
    with open(OUTPUT_CSV, mode="a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not archivo_existe:
            writer.writeheader()
        writer.writerow(registro)


async def extraer_por_etiqueta(page, label: str) -> str:
    """
    Función de parsing resiliente basada en texto del DOM.
    Busca una celda o etiqueta que coincida con el nombre del campo en portugués (ej. 'Bairro')
    y extrae el contenido del elemento contiguo (su hermano en el DOM).
    """
    try:
        # Buscar el elemento que contiene la etiqueta exacta de forma insensible a mayúsculas/minúsculas
        element = await page.query_selector(f"text='{label}'")
        if element:
            # Evaluar javascript en el contexto de la página para obtener el hermano de la derecha
            value = await page.evaluate(
                """(el) => {
                    const parent = el.parentElement;
                    if (parent) {
                        const children = Array.from(parent.children);
                        const idx = children.indexOf(el);
                        // Retorna el texto del siguiente elemento hermano
                        if (idx !== -1 && children[idx + 1]) {
                            return children[idx + 1].innerText.trim();
                        }
                    }
                    return '';
                }""",
                element
            )
            return value
    except Exception as e:
        # Fallback silencioso ante discrepancias en el DOM
        pass
    return ""


async def parse_reporte(page) -> dict:
    """
    Extrae la información catastral de la página del reporte resultante.
    Intenta buscar por selectores CSS específicos o cae en la búsqueda por etiquetas del DOM.
    """
    # 1. Definir diccionarios con selectores CSS específicos (si existen)
    # 2. Utilizar el parsing por etiquetas de texto como estrategia resiliente
    inscripcion = await extraer_por_etiqueta(page, "Inscrição")
    tipo_inmueble = await extraer_por_etiqueta(page, "Tipo de Imóvel") or await extraer_por_etiqueta(page, "Tipo")
    logradouro = await extraer_por_etiqueta(page, "Logradouro") or await extraer_por_etiqueta(page, "Endereço")
    bairro = await extraer_por_etiqueta(page, "Bairro")
    unidade = await extraer_por_etiqueta(page, "Unidade")
    cep = await extraer_por_etiqueta(page, "CEP")
    
    return {
        "Inscripción": inscripcion,
        "Tipo de Inmueble": tipo_inmueble,
        "Logradouro": logradouro,
        "Bairro": bairro,
        "Unidade": unidade,
        "CEP": cep
    }


async def detectar_bloqueos(page) -> bool:
    """
    Verifica si en la página actual se ha desplegado un desafío de CAPTCHA
    o un mensaje de bloqueo perimetral.
    """
    # Buscar presencia de iframes de recaptcha, hcaptcha o elementos similares
    for selector in [SELECTOR_CAPTCHA, "iframe[title*='reCAPTCHA']", ".g-recaptcha"]:
        try:
            element = await page.query_selector(selector)
            if element and await element.is_visible():
                return True
        except Exception:
            pass
    return False


# ==============================================================================
# FLUJO PRINCIPAL DE SCRAPING ASÍNCRONO
# ==============================================================================

async def main():
    # Asegurar la existencia de la carpeta de descargas
    os.makedirs(PDF_DIR, exist_ok=True)

    # Generar y barajar la lista de IDs para evitar accesos secuenciales
    id_list = list(range(START_ID, END_ID + 1))
    random.shuffle(id_list)

    print("=" * 80)
    print("INICIANDO EXTRACCIÓN CATASTRAL CON SIMULACIÓN HUMANA (STEALTH MODE)")
    print(f"Total de IDs a evaluar: {len(id_list)} (Barajados en orden aleatorio)")
    print(f"Modo Headless: {HEADLESS}")
    print(f"Carpeta de descargas: {PDF_DIR}")
    print(f"Rango de delay aleatorio: {MIN_DELAY_SECS} a {MAX_DELAY_SECS} segundos")
    print(f"Archivo de salida: {OUTPUT_CSV}")
    print("=" * 80)

    async with async_playwright() as p:
        # Lanzar el navegador configurando el modo headless y argumentos anti-fingerprinting
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        
        # Contexto con User-Agent residencial
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800}
        )
        
        page = await context.new_page()
        
        # Registrar manejador de diálogos (alertas, confirmaciones, etc.)
        async def handle_dialog(dialog):
            print(f"    [diálogo] Tipo: {dialog.type} | Mensaje: {dialog.message}")
            await dialog.dismiss() if dialog.type == "beforeunload" else await dialog.accept()
        page.on("dialog", handle_dialog)
        
        # Registrar mensajes de la consola del navegador
        page.on("console", lambda msg: print(f"    [console] {msg.type}: {msg.text}"))
        
        # Definir variable de seguimiento para cierres (closures) de eventos y control de descargas
        current_id = START_ID
        estado_descarga = {"pdf_descargado": False}
        
        # Registrar descargas en el contexto (captura descargas de cualquier pestaña)
        async def handle_download(download):
            filename = f"reporte_{current_id}.pdf"
            filepath = os.path.join(PDF_DIR, filename)
            print(f"    [descarga] Se detectó descarga del archivo: {download.suggested_filename}")
            await download.save_as(filepath)
            print(f"    [descarga] Archivo guardado con éxito en: {filepath}")
            estado_descarga["pdf_descargado"] = True
        context.on("download", handle_download)
        
        # Interceptor de respuestas de red para capturar PDFs transmitidos inline
        async def handle_response(response):
            try:
                ct = response.headers.get("content-type", "").lower()
                if "application/pdf" in ct or "pdf" in response.url.lower():
                    print(f"    [debug] Interceptada respuesta PDF en: {response.url}")
                    pdf_bytes = await response.body()
                    filename = os.path.join(PDF_DIR, f"reporte_{current_id}.pdf")
                    with open(filename, "wb") as f:
                        f.write(pdf_bytes)
                    print(f"    [debug] PDF inline guardado con éxito en: {filename}")
                    estado_descarga["pdf_descargado"] = True
            except Exception:
                pass
        context.on("response", handle_response)
        
        # Ejecutar script anti-automatización para enmascarar la variable navigator.webdriver
        await page.add_init_script(
            "const newProto = navigator.__proto__; "
            "delete newProto.webdriver; "
            "navigator.__proto__ = newProto;"
        )

        descargas_exitosas = 0

        for current_id in id_list:
            # 1. Comprobar si el archivo PDF ya existe en la carpeta de descargas
            filename_check = os.path.join(PDF_DIR, f"reporte_{current_id}.pdf")
            if os.path.exists(filename_check):
                print(f"[-] Saltando ID {current_id} (Ya fue descargado: {filename_check})")
                continue

            # Reiniciar estado de descarga para este ID
            estado_descarga["pdf_descargado"] = False
            
            # Formatear el entero como string estricto de 15 caracteres rellenado con ceros a la izquierda
            formatted_id = f"{current_id:015d}"
            print(f"\n[+] Procesando ID {current_id:5d} -> '{formatted_id}'")

            try:
                # Navegar a la URL del formulario
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                print(f"    [debug] URL actual: {page.url}")
                print(f"    [debug] Título de página: {await page.title()}")
                await page.screenshot(path="page_initial.png")
                print("    [debug] Captura de pantalla 'page_initial.png' guardada.")
                
                # Guardar el HTML para inspeccionar los selectores reales
                html_content = await page.content()
                with open("page_initial.html", "w", encoding="utf-8") as f:
                    f.write(html_content)
                print("    [debug] HTML guardado en 'page_initial.html'.")
                
                # Esperar a que el input de identificación esté disponible
                await page.wait_for_selector(SELECTOR_INPUT_ID, timeout=10000)
                
                # Rellenar el formulario simulando interacción humana para disparar eventos onchange/onblur
                await page.focus(SELECTOR_INPUT_ID)
                await page.click(SELECTOR_INPUT_ID)
                
                # Seleccionar todo y borrar
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                
                # Escribir con un pequeño retraso
                await page.keyboard.type(formatted_id, delay=80)
                
                # Forzar blur presionando Tab para que GeneXus actualice el estado GXState
                await page.keyboard.press("Tab")
                
                # Espera corta para procesamiento de eventos de JS
                await page.wait_for_timeout(1000)
                
                # Simular clic en el botón de consultar y esperar a la nueva pestaña (popup)
                is_new_tab = False
                active_page = page
                try:
                    async with page.expect_popup(timeout=8000) as popup_info:
                        await page.click(SELECTOR_BTN_CONSULTAR)
                    active_page = await popup_info.value
                    is_new_tab = True
                    print("    [debug] Se detectó una nueva pestaña (popup). Cambiando contexto...")
                    
                    # Esperar a que la URL deje de ser about:blank y se cargue la página
                    for attempt in range(15):
                        if active_page.url and active_page.url != "about:blank":
                            break
                        await page.wait_for_timeout(500)
                    
                    try:
                        await active_page.wait_for_load_state("load", timeout=15000)
                    except Exception:
                        pass
                except PlaywrightTimeoutError:
                    print("    [debug] No se detectó ninguna pestaña nueva (popup). Continuando en la página principal...")
                    await page.wait_for_load_state("networkidle")
                
                print(f"    [debug] URL del reporte/resultado: {active_page.url}")
                try:
                    print(f"    [debug] Título del reporte/resultado: {await active_page.title()}")
                except Exception as e:
                    print(f"    [debug] No se pudo obtener el título: {e}")
                    
                try:
                    await active_page.screenshot(path="page_after_search.png")
                    print("    [debug] Captura de pantalla 'page_after_search.png' guardada.")
                except Exception as e:
                    print(f"    [debug] No se pudo tomar captura de pantalla: {e}")
                
                try:
                    with open("page_after_search.html", "w", encoding="utf-8") as f:
                        f.write(await active_page.content())
                    print("    [debug] HTML guardado en 'page_after_search.html'.")
                except Exception as e:
                    print(f"    [debug] No se pudo guardar el HTML: {e}")

                # --- CONTROL DE RESILIENCIA Y ERRORES ---
                
                # 0. Comprobar si el PDF del reporte ya fue descargado por los interceptores
                if estado_descarga["pdf_descargado"]:
                    print(f"    [✓] Reporte PDF para el ID {current_id} descargado y guardado con éxito.")
                    
                    # Guardar registro en el CSV para constancia de la descarga exitosa
                    datos_pdf = {
                        "Inscripción": formatted_id,
                        "Tipo de Inmueble": "Ver en PDF",
                        "Logradouro": f"Reporte descargado (reporte_{current_id}.pdf)",
                        "Bairro": "N/D",
                        "Unidade": "N/D",
                        "CEP": "N/D"
                    }
                    guardar_en_csv(datos_pdf)
                    
                    descargas_exitosas += 1
                    
                    if is_new_tab:
                        await active_page.close()
                        
                    # --- LÓGICA DE PAUSA HUMANA ALEATORIA ---
                    if descargas_exitosas % PAUSA_CADA_N_DESCARGAS == 0:
                        pausa_minutos = random.randint(PAUSA_MIN_MINUTOS, PAUSA_MAX_MINUTOS)
                        print(f"\n[i] Simulación Humana: Se completaron {descargas_exitosas} descargas.")
                        print(f"[i] Tomando un descanso de {pausa_minutos} minutos antes de continuar...")
                        await asyncio.sleep(pausa_minutos * 60)
                    else:
                        delay_actual = random.uniform(MIN_DELAY_SECS, MAX_DELAY_SECS)
                        print(f"    [i] Espera aleatoria de cortesía: {delay_actual:.2f} segundos...")
                        await asyncio.sleep(delay_actual)
                    continue
                
                # 1. Comprobar presencia de CAPTCHA
                if await detectar_bloqueos(active_page):
                    screenshot_path = "captcha_detected.png"
                    await active_page.screenshot(path=screenshot_path)
                    print(f"\n[!] ALERTA: CAPTCHA o Bloqueo detectado al procesar ID {current_id}.")
                    print(f"[!] Captura de pantalla guardada como '{screenshot_path}'.")
                    print("[!] Pausando ejecución para evitar penalizaciones del servidor.")
                    
                    # Pausa el hilo de ejecución asíncrona solicitando intervención manual en terminal
                    if not HEADLESS:
                        print("[i] Por favor, resuelva el desafío en el navegador visual abierto.")
                    input("Presione ENTER en esta consola una vez resuelto el desafío para continuar, o Ctrl+C para abortar...")
                    if is_new_tab:
                        await active_page.close()
                    continue

                # 2. Comprobar si el inmueble no existe en el sistema
                error_not_found = await active_page.query_selector(SELECTOR_ERROR_NOT_FOUND)
                if error_not_found and await error_not_found.is_visible():
                    print(f"    [-] ID {current_id} no encontrado en el sistema ('Imóvel não encontrado').")
                    if is_new_tab:
                        await active_page.close()
                    # Retraso aleatorio corto para simular búsqueda humana fallida
                    delay_fallido = random.uniform(10, 30)
                    print(f"    [i] Espera aleatoria por ID no encontrado: {delay_fallido:.2f} segundos...")
                    await asyncio.sleep(delay_fallido)
                    continue

                # 3. Esperar la carga de la tabla/reporte de resultados
                try:
                    await active_page.wait_for_selector(SELECTOR_RESULT_CONTAINER, timeout=8000)
                except PlaywrightTimeoutError:
                    # Si no hay error explícito pero tampoco se cargó el contenedor, verificar de nuevo si existe error
                    error_check = await active_page.query_selector(SELECTOR_ERROR_NOT_FOUND)
                    if error_check and await error_check.is_visible():
                        print(f"    [-] ID {current_id} no encontrado (Timeout en contenedor).")
                        if is_new_tab:
                            await active_page.close()
                        # Retraso por búsqueda fallida
                        delay_fallido = random.uniform(10, 30)
                        await asyncio.sleep(delay_fallido)
                        continue
                    else:
                        raise PlaywrightTimeoutError("El contenedor de resultados no apareció dentro del tiempo estimado.")

                # --- EXTRACCIÓN Y PERSISTENCIA ---
                
                # Realizar el parsing de los datos del inmueble
                datos = await parse_reporte(active_page)
                
                # Asegurar que al menos tengamos la inscripción o algún campo para guardar
                if datos["Inscripción"] or datos["Logradouro"]:
                    # Si la inscripción vino vacía del DOM, usamos el ID que consultamos como fallback
                    if not datos["Inscripción"]:
                        datos["Inscripción"] = formatted_id
                        
                    guardar_en_csv(datos)
                    print(f"    [✓] Registro guardado con éxito: {datos['Logradouro']} - {datos['Bairro']}")
                else:
                    print(f"    [!] ID {current_id} cargó reporte pero los datos están vacíos o no estructurados.")

                # Cerrar la pestaña si era una nueva
                if is_new_tab:
                    await active_page.close()

                # --- CORTESÍA DEL SERVIDOR (Rate Limiting) ---
                delay_actual = random.uniform(MIN_DELAY_SECS, MAX_DELAY_SECS)
                print(f"    [i] Espera aleatoria de cortesía: {delay_actual:.2f} segundos...")
                await asyncio.sleep(delay_actual)

            except PlaywrightTimeoutError:
                print(f"    [!] Timeout al procesar ID {current_id}. El servidor tarda en responder o cambió la estructura.")
                delay_err = random.uniform(20, 60)
                print(f"    [i] Espera aleatoria tras error de timeout: {delay_err:.2f} segundos...")
                await asyncio.sleep(delay_err)
            except Exception as e:
                print(f"    [!] Error inesperado al procesar ID {current_id}: {str(e)}")
                delay_err = random.uniform(20, 60)
                print(f"    [i] Espera aleatoria tras error: {delay_err:.2f} segundos...")
                await asyncio.sleep(delay_err)

        print("\n" + "=" * 80)
        print(f"PROCESO FINALIZADO. Resultados almacenados en '{OUTPUT_CSV}'.")
        print("=" * 80)
        
        # Cerrar el navegador y limpiar el contexto de ejecución
        await context.close()
        await browser.close()


if __name__ == "__main__":
    # Iniciar el loop de eventos asíncronos para ejecutar el script
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Ejecución interrumpida por el usuario.")
        sys.exit(0)
