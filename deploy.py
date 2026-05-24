#!/usr/bin/env python3
"""
deploy.py — Despliega remadbot en una Raspberry Pi vía SSH.

Uso:
    python3 deploy.py

Requisitos en la máquina local:
    pip install paramiko
"""

import getpass
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("Instalando paramiko...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko", "-q"])
    import paramiko

# ── Ficheros a copiar ─────────────────────────────────────────────────────────

HERE = Path(__file__).parent
FILES = [
    "remadbot.py",
    "watchdog.sh",
    "environment.yml",
    ".env",            # si no existe se usará .env.example y se avisará
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def preguntar(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    resp = input(f"{prompt}{suffix}: ").strip()
    return resp or default


def ok(msg: str):   print(f"  ✅ {msg}")
def info(msg: str): print(f"  ℹ️  {msg}")
def err(msg: str):  print(f"  ❌ {msg}")


def ssh_run(client: paramiko.SSHClient, cmd: str, check: bool = True,
            verbose: bool = False) -> tuple[int, str, str]:
    """Ejecuta un comando SSH y devuelve (exit_code, stdout, stderr)."""
    _, stdout, stderr = client.exec_command(cmd, get_pty=verbose)
    out_lines = []
    if verbose:
        # Streaming en tiempo real para comandos lentos
        while True:
            line = stdout.readline()
            if not line:
                break
            line = line.rstrip()
            if line:
                print(f"    {line}")
                out_lines.append(line)
        exit_code = stdout.channel.recv_exit_status()
        errout = ""
    else:
        exit_code = stdout.channel.recv_exit_status()
        out_lines = [stdout.read().decode().strip()]
        errout = stderr.read().decode().strip()

    out = "\n".join(out_lines)
    if check and exit_code != 0:
        err(f"Comando falló (exit {exit_code}): {cmd}")
        if errout:
            print(f"     stderr: {errout[:300]}")
        sys.exit(1)
    return exit_code, out, errout


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  remadbot — Deploy a Raspberry Pi")
    print("=" * 55)
    print()

    # ── Recoger parámetros ────────────────────────────────────────────────────
    ip       = preguntar("IP de la Raspberry Pi")
    usuario  = preguntar("Usuario SSH", "pi")
    password = getpass.getpass(f"  Contraseña SSH para {usuario}@{ip}: ")
    dest_dir = preguntar("Directorio destino en la Pi", f"/home/{usuario}/remadbot")
    mamba    = preguntar("Ruta de miniforge/mambaforge en la Pi", f"/home/{usuario}/miniforge3")
    cron_int = preguntar("Intervalo del watchdog en cron (minutos)", "5")

    print()

    # ── Verificar .env ────────────────────────────────────────────────────────
    env_file = HERE / ".env"
    if not env_file.exists():
        if (HERE / ".env.example").exists():
            err(".env no encontrado — copia .env.example a .env y rellénalo antes de desplegar")
        else:
            err(".env no encontrado")
        sys.exit(1)

    env_content = env_file.read_text()
    token_line = next((l for l in env_content.splitlines() if l.startswith("TELEGRAM_BOT_TOKEN=")), "")
    token_val = token_line.split("=", 1)[1].strip() if "=" in token_line else ""
    if not token_val or "xxx" in token_val.lower():
        err("TELEGRAM_BOT_TOKEN está vacío o sin rellenar en .env")
        sys.exit(1)

    # ── Conectar SSH ──────────────────────────────────────────────────────────
    print(f"Conectando a {usuario}@{ip}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=usuario, password=password, timeout=15)
        client.get_transport().set_keepalive(30)  # evita que la conexión caiga en ops largas
        ok(f"Conexión SSH establecida")
    except Exception as e:
        err(f"No se pudo conectar: {e}")
        sys.exit(1)

    sftp = client.open_sftp()

    # ── Crear directorio destino ──────────────────────────────────────────────
    print(f"\nCreando directorio {dest_dir}...")
    ssh_run(client, f"mkdir -p {dest_dir}")
    ok(f"Directorio listo")

    # ── Copiar ficheros ───────────────────────────────────────────────────────
    print(f"\nCopiando ficheros...")
    for fname in FILES:
        local = HERE / fname
        remote = f"{dest_dir}/{fname}"
        if not local.exists():
            info(f"Omitiendo {fname} (no existe localmente)")
            continue
        sftp.put(str(local), remote)
        ok(f"{fname}")

    # watchdog.sh: actualizar rutas con las de esta Pi
    print("\nActualizando rutas en watchdog.sh...")
    _, wdog, _ = ssh_run(client, f"cat {dest_dir}/watchdog.sh")
    wdog = wdog.replace("/home/hachas/remadbot/remadbot", dest_dir)
    wdog = wdog.replace("/home/hachas/miniforge3", mamba)
    wdog = wdog.replace("$HOME/remadbot/remadbot", dest_dir)
    wdog = wdog.replace("$HOME/miniforge3", mamba)

    # Escribir el watchdog actualizado
    with sftp.open(f"{dest_dir}/watchdog.sh", "w") as f:
        f.write(wdog)
    ssh_run(client, f"chmod +x {dest_dir}/watchdog.sh")
    ok("watchdog.sh actualizado y con permisos de ejecución")

    # ── Crear entorno conda/mamba ─────────────────────────────────────────────
    print(f"\nCreando entorno conda 'remadbot'...")

    # Detectar si mamba o conda está disponible
    code, _, _ = ssh_run(client, f"test -f {mamba}/bin/mamba", check=False)
    bin_name = "mamba" if code == 0 else "conda"
    info(f"Usando: {bin_name}")

    # Prefijo que inicializa conda antes de cada comando
    conda_init = f"source {mamba}/etc/profile.d/conda.sh && "

    # Comprobar si el entorno ya existe
    code, out, _ = ssh_run(client, f"{conda_init} conda env list", check=False)
    if "remadbot" in out:
        info("Entorno 'remadbot' ya existe — actualizando dependencias (puede tardar unos minutos...)")
        ssh_run(client, f"{conda_init} {bin_name} env update -y -n remadbot -f {dest_dir}/environment.yml --prune", verbose=True)
    else:
        info("Creando entorno desde cero en la Pi (puede tardar 5-10 min)...")
        ssh_run(client, f"{conda_init} {bin_name} env create -y -f {dest_dir}/environment.yml", verbose=True)
    ok("Entorno conda listo")

    # ── Configurar cron ───────────────────────────────────────────────────────
    print(f"\nConfigurando cron (watchdog cada {cron_int} min)...")
    cron_line = f"*/{cron_int} * * * * {dest_dir}/watchdog.sh >> {dest_dir}/watchdog.log 2>&1"

    # Leer crontab actual y añadir si no está ya
    code, current_cron, _ = ssh_run(client, "crontab -l 2>/dev/null || echo ''", check=False)
    if "watchdog.sh" in current_cron:
        info("Entrada de cron ya existe — no se modifica")
    else:
        new_cron = (current_cron.strip() + "\n" + cron_line + "\n").lstrip()
        ssh_run(client, f'echo "{new_cron}" | crontab -')
        ok(f"Cron añadido: {cron_line}")

    # ── Arrancar el bot ───────────────────────────────────────────────────────
    print(f"\nArrancando remadbot...")
    python_bin = f"{mamba}/envs/remadbot/bin/python3"

    # Parar instancia previa si la hay
    ssh_run(client, "pkill -f remadbot.py 2>/dev/null || true", check=False)

    # Lanzar en background: escribir un script temporal y ejecutarlo
    launch_script = f"/tmp/start_remadbot.sh"
    ssh_run(client, (
        f"printf '#!/bin/bash\\ncd {dest_dir}\\n"
        f"nohup {python_bin} remadbot.py > {dest_dir}/remadbot.log 2>&1 &\\n"
        f"echo $!' > {launch_script} && chmod +x {launch_script}"
    ))
    # Ejecutar con timeout corto — no esperamos a que el proceso hijo termine
    transport = client.get_transport()
    channel = transport.open_session()
    channel.exec_command(f"bash {launch_script}")
    channel.settimeout(5)
    try:
        channel.recv_exit_status()
    except Exception:
        pass
    channel.close()

    import time; time.sleep(3)

    # Verificar que arrancó
    code, pid, _ = ssh_run(client, "pgrep -f remadbot.py", check=False)
    if code == 0:
        ok(f"remadbot corriendo (PID {pid})")
    else:
        err("remadbot no arrancó — revisa el log:")
        _, log_tail, _ = ssh_run(client, f"tail -20 {dest_dir}/remadbot.log", check=False)
        print(log_tail)

    # ── Resumen ───────────────────────────────────────────────────────────────
    sftp.close()
    client.close()

    print()
    print("=" * 55)
    print("  Deploy completado")
    print("=" * 55)
    print(f"  Directorio : {dest_dir}")
    print(f"  Log del bot: {dest_dir}/remadbot.log")
    print(f"  Watchdog   : cada {cron_int} minutos vía cron")
    print()
    print("  Para ver el log en tiempo real:")
    print(f"    ssh {usuario}@{ip} 'tail -f {dest_dir}/remadbot.log'")
    print()


if __name__ == "__main__":
    main()
