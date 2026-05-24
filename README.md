<div align="center">
  <h1>♻️ ReMAD Bot</h1>
  <p><b>Monitorización automatizada y alertas en tiempo real para el catálogo de economía circular del Ayuntamiento de Madrid.</b></p>
</div>

---

## 📖 Descripción General

**ReMAD Bot** es un agente automatizado diseñado para integrarse con Telegram y alertar a los usuarios sobre la disponibilidad de nuevos objetos en el catálogo de ReMAD (Ayuntamiento de Madrid). A través de un sondeo eficiente, permite configurar alertas tempranas basadas en palabras clave, asegurando que no pierdas oportunidades de darle una segunda vida a objetos de tu interés.

---

## ⚙️ Arquitectura y Lógica de Funcionamiento

El proyecto se compone de tres piezas fundamentales operando en conjunto:

### 🐍 1. Motor de Extracción (`remadbot.py`)
Es el corazón del sistema, desarrollado en Python, encargado de la interacción con la plataforma y la gestión de alertas.
*   **Interacción de API (Sondeo/Polling)**: Establece conexiones HTTP persistentes (vía `requests.Session`) contra el endpoint de catálogo de ReMAD (`REMAD_RSP/api/v1`).
*   **Filtrado Inteligente**: Implementa un doble filtro. Primero, a nivel de servidor (si se definen `KEYWORDS`) limitando los resultados devueltos, y posteriormente a nivel local, descartando todo elemento cuyo atributo `reserved` sea `True`.
*   **Gestión de Estado (Deduplicación)**: Emplea una base de datos ligera basada en un archivo plano configurado en `.env` (ej. `sent_hashes.txt`). Cada artículo posee un `hash` único; si el hash ya ha sido notificado en el pasado, el bot lo descarta de forma segura.
*   **Notificaciones Enriquecidas**: Se comunica con la API de Telegram para enviar mensajes en formato `MarkdownV2`, adjuntando metadatos estructurados (ubicación, categoría) y previsualización de imágenes cuando el sistema dispone de ellas.

### 🛡️ 2. Supervisor y Control Horario (`watchdog.sh`)
Un script robusto en Bash que ejerce como demonio supervisor (Watchdog) para garantizar la resiliencia del bot y su adecuación estricta a los horarios reales de los Puntos Limpios.
*   **Conciencia Temporal**: Calcula dinámicamente si el minuto actual del día corresponde al horario de apertura de las instalaciones.
*   **Gestión del Ciclo de Vida**: 
    *   *En Horario de Apertura:* Verifica la existencia del proceso en segundo plano (usando `kill -0` sobre el PID guardado). Si ha caído por un error crítico, lo revive mediante `nohup`.
    *   *Fuera de Horario:* Apaga limpiamente el proceso de Python. Esto evita peticiones inútiles a la API (ya que los operarios no actualizan el catálogo cuando el centro está cerrado), ahorrando recursos de CPU, red y reduciendo la carga en los servidores del Ayuntamiento.
*   **Soporte para Festivos**: Consulta un archivo local `festivos.txt` (con formato de fecha `YYYY-MM-DD`). Si el día de hoy coincide con uno de la lista, el supervisor aplica automáticamente el mismo horario reducido que se utiliza los domingos.

### 🚀 3. Despliegue Automatizado (`deploy.py`)
Una herramienta de orquestación que utiliza `paramiko` para realizar despliegues limpios vía SSH. Es capaz de conectarse a servidores remotos (como una Raspberry Pi), transferir los archivos, inicializar entornos virtuales de Mamba/Conda y programar la tarea cron de forma completamente desatendida.

---

## 🕵️ Análisis Técnico y Superficie de Exposición

El funcionamiento ininterrumpido y eficiente de este bot es posible gracias a cómo está diseñada la arquitectura del portal oficial. El script no explota una vulnerabilidad grave de ejecución de código o inyección de base de datos, sino que se beneficia de una **exposición de API pública (Falta de Autenticación Fuerte y Rate Limiting Permisivo)**:

1.  **Endpoints Abiertos (`No Auth`)**: La API REST subyacente que alimenta el frontend de ReMAD no exige mecanismos estrictos de autorización como tokens JWT, cookies de sesión firmadas o validación de origen estricta (CORS/CSRF) para el consumo del catálogo. Permite realizar consultas programáticas POST de forma esencialmente anónima.
2.  **Ausencia de Rate Limiting Agresivo**: Aunque el bot aplica pausas (`time.sleep()`) por cortesía y prevención de saturación, la infraestructura de ReMAD permite iterar por múltiples páginas de resultados en ventanas de tiempo muy cortas sin aplicar bloqueos IP inmediatos, facilitando enormemente el consumo vía API.
3.  **Recursos Estáticos Predictibles**: El acceso a los recursos multimedia (fotografías de los objetos) y la composición de los enlaces a cada ficha se basan en identificadores devueltos en texto plano por la API. Estos recursos no están protegidos tras firmas temporales o mecanismos de autenticación, permitiendo al bot extraerlos e incrustarlos en Telegram directamente.

---

## ⏰ Modo de Operación y Configuración Cron

El bot ha sido diseñado para funcionar de manera completamente autónoma "en la sombra", activándose y desactivándose según las franjas horarias reales en las que el catálogo puede sufrir modificaciones.

### Horarios de Operación
*   📅 **Lunes a Viernes**: 15:00h – 20:00h
*   📅 **Sábados**: 08:00h – 20:00h
*   📅 **Domingos y Festivos**: 09:00h – 14:00h

### Configuración en Crontab
La resiliencia del sistema depende de ejecutar el supervisor `watchdog.sh` con alta frecuencia (cada 2 o 5 minutos). 

Para configurarlo manualmente, añade la siguiente línea a tu crontab (`crontab -e`):

```bash
# Comprobar el estado del bot e iniciarlo/apagarlo cada 2 minutos
*/2 * * * * /ruta/absoluta/a/tu/proyecto/watchdog.sh >> /ruta/absoluta/a/tu/proyecto/watchdog.log 2>&1
```

*(Nota: Si usas el script `deploy.py`, esta configuración del cron se realiza automáticamente en el servidor remoto).*

---

## 🛠️ Instalación y Configuración Rápida

1. **Clonar y Preparar el Entorno**:
   ```bash
   git clone <url-del-repo> remadbot
   cd remadbot
   cp .env.example .env
   ```

2. **Definir Variables de Entorno** (Edita el archivo `.env`):
   *   `TELEGRAM_BOT_TOKEN`: Token obtenido a través de [@BotFather](https://t.me/BotFather).
   *   `TELEGRAM_CHAT_ID`: El identificador de tu usuario o del grupo donde se enviarán las alertas.
   *   `KEYWORDS`: Lista separada por comas (ej. `bici,ordenador,mesa,silla`). Si lo dejas vacío, el bot notificará **absolutamente todos** los objetos nuevos.

3. **Despliegue Asistido (Para Raspberry Pi u otro servidor Linux vía SSH)**:
   Si deseas automatizar la instalación en remoto, instala las dependencias locales y ejecuta el script:
   ```bash
   pip install paramiko
   python3 deploy.py
   ```
   *Sigue las instrucciones interactivas en tu terminal para completar el despliegue.*

---

## 📂 Estructura del Proyecto

```text
.
├── remadbot.py        # Motor principal en Python (Scraping, Filtrado, Telegram)
├── watchdog.sh        # Supervisor Bash de estado del proceso y reglas horarias
├── deploy.py          # Orquestador de despliegue remoto vía SSH
├── environment.yml    # Archivo de dependencias para entornos Conda/Mamba
├── .env.example       # Plantilla de variables de entorno requeridas
├── festivos.txt       # (Opcional) Listado de fechas excepcionales (YYYY-MM-DD)
└── sent_hashes.txt    # (Auto-generado) Registro de IDs notificados
```