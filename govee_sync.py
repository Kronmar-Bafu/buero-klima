#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak", "cryptography"]
# ///
"""Govee H5179: gespeicherten Verlauf per Bluetooth LE abholen und als CSV ablegen.

Der Sensor speichert rund 20 Tage Minutenwerte. Dieses Skript sucht Sensoren in
Reichweite, verbindet sich, laedt alles seit dem letzten gespeicherten Zeitpunkt
und haengt es an monatliche CSV-Dateien an:

    data/<Sensorname>/<JJJJ-MM>.csv      Spalten: time,temp_c,humidity_pct  (Zeit in UTC)
    data/index.json                      Uebersicht fuer das Dashboard

Ist kein Sensor in Reichweite, passiert nichts (Exit-Code 0).

Aufruf mit uv (die Abhaengigkeiten stehen oben im Skript):
    uv run govee_sync.py --max-minutes 30 --dry-run -v    # Test, schreibt nichts
    uv run govee_sync.py                                   # Verlauf abholen
    uv run govee_sync.py --push                            # zusaetzlich committen und pushen

Die Govee-App muss geschlossen sein, sonst ist der Sensor belegt.

Protokoll nach https://github.com/wcbonner/GoveeBTTempLogger (MIT-Lizenz).
"""

import argparse
import asyncio
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

UUID_BASE = "494e5445-4c4c-495f-524f-434b535f{}"
CH_COMMAND = UUID_BASE.format("2011")   # Geraetebefehle
CH_REQUEST = UUID_BASE.format("2012")   # Verlauf anfordern, Status
CH_DATA = UUID_BASE.format("2013")      # Verlaufsdaten
CH_AUTH_NOTIFY = "00010203-0405-0607-0809-0a0b0c0d2b10"
CH_AUTH_WRITE = "00010203-0405-0607-0809-0a0b0c0d2b11"

# Fest in der Govee-App hinterlegter Schluessel fuer den Sitzungsaufbau.
PRE_SHARED_KEY = b"MakingLifeSmarte"

SCRIPT_DIR = Path(__file__).resolve().parent
MAX_MINUTES = 0xFFFF
IDLE_TIMEOUT_S = 20       # so lange ohne Datenpaket, dann gilt der Download als beendet
KEEPALIVE_EVERY = 75      # Datenpakete zwischen zwei Keep-Alives


# ---------------------------------------------------------------- Paketformat

def with_checksum(payload: bytes) -> bytes:
    """Auf 20 Bytes auffuellen, letztes Byte = XOR der ersten 19."""
    buf = bytearray(payload.ljust(20, b"\x00")[:20])
    buf[19] = 0
    for b in buf[:19]:
        buf[19] ^= b
    return bytes(buf)


def checksum_ok(packet: bytes) -> bool:
    x = 0
    for b in packet[:19]:
        x ^= b
    return len(packet) == 20 and x == packet[19]


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def _aes_ecb(key: bytes, block: bytes, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(block) + op.finalize()


def encrypt_packet(packet: bytes, key: bytes | None) -> bytes:
    """Bytes 0-15 mit AES-128-ECB, Bytes 16-19 mit RC4, jeweils gleicher Schluessel."""
    if key is None:
        return packet
    return _aes_ecb(key, packet[:16], True) + _rc4(key, packet[16:20])


def decrypt_packet(packet: bytes, key: bytes | None) -> bytes:
    if key is None or len(packet) != 20:
        return packet
    return _aes_ecb(key, packet[:16], False) + _rc4(key, packet[16:20])


def decode_history_packet(packet: bytes, t0: int):
    """Ein Datenpaket -> (offset, [(unix_zeit, temp_c, feuchte_pct), ...]).

    Bytes 0-1: Offset in Minuten vor t0 (big endian) fuer den ersten Wert.
    Danach 6 Werte zu je 3 Bytes, jeder eine Minute juenger als der vorige.
    Ein Wert n kodiert Temperatur = n / 10000 und Feuchte = (n % 1000) / 10,
    das hoechste Bit steht fuer negative Temperaturen. ffffff = kein Wert.
    """
    offset = int.from_bytes(packet[0:2], "big")
    rows = []
    off = offset
    for i in range(2, 20, 3):
        if off <= 0:
            break
        n = int.from_bytes(packet[i:i + 3], "big")
        if n != 0xFFFFFF:
            negative = bool(n & 0x800000)
            n &= 0x7FFFFF
            temp = n / 10000
            hum = (n % 1000) / 10
            if negative:
                temp = -temp
            if -40 <= temp <= 85 and 0 <= hum <= 100:
                rows.append((t0 - 60 * off, round(temp, 1), round(hum, 1)))
        off -= 1
    return offset, rows


def decode_advertisement(mfg: dict):
    """Aktueller Messwert aus dem Advertisement: (temp_c, feuchte_pct, batterie_pct) oder None."""
    p = mfg.get(0x0001)
    if p is None or len(p) != 6:
        return None
    n = int.from_bytes(p[2:5], "big")
    negative = bool(n & 0x800000)
    n &= 0x7FFFFF
    temp = n / 10000
    return (-temp if negative else temp), (n % 1000) / 10, p[5]


# ------------------------------------------------------------------ CSV-Ablage

def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_stored_time(sensor_dir: Path) -> int | None:
    files = sorted(sensor_dir.glob("????-??.csv"))
    for f in reversed(files):
        lines = f.read_text().strip().splitlines()
        if len(lines) > 1:
            stamp = lines[-1].split(",")[0]
            return int(datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
                       .replace(tzinfo=timezone.utc).timestamp())
    return None


def append_rows(sensor_dir: Path, rows) -> None:
    sensor_dir.mkdir(parents=True, exist_ok=True)
    by_month = {}
    for ts, temp, hum in rows:
        month = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")
        by_month.setdefault(month, []).append((iso(ts), temp, hum))
    for month, items in by_month.items():
        path = sensor_dir / f"{month}.csv"
        is_new = not path.exists()
        with path.open("a", newline="") as fh:
            writer = csv.writer(fh)
            if is_new:
                writer.writerow(["time", "temp_c", "humidity_pct"])
            writer.writerows(items)


def write_index(data_dir: Path, battery: dict) -> None:
    index_path = data_dir / "index.json"
    old = {}
    if index_path.exists():
        try:
            old = json.loads(index_path.read_text()).get("sensors", {})
        except (ValueError, AttributeError):
            old = {}
    sensors = {}
    for sensor_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        files = sorted(f.name for f in sensor_dir.glob("????-??.csv"))
        if not files:
            continue
        last = last_stored_time(sensor_dir)
        name = sensor_dir.name
        sensors[name] = {
            "files": files,
            "last": iso(last) if last else None,
            "battery_pct": battery.get(name, old.get(name, {}).get("battery_pct")),
        }
    if sensors == old:
        return
    index_path.write_text(json.dumps(
        {"updated": iso(int(time.time())), "sensors": sensors}, indent=2) + "\n")


# ------------------------------------------------------------------------- Git

def git_publish(repo: Path, data_dir: Path) -> None:
    """Neue Messwerte committen und pushen. Ein fehlgeschlagener Push ist kein
    Fehler: der Commit bleibt lokal und geht beim naechsten Lauf mit hoch."""
    def git(*cmd):
        return subprocess.run(["git", "-C", str(repo), *cmd],
                              capture_output=True, text=True)

    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        print(f"--push: {repo} ist kein Git-Repository, ueberspringe")
        return
    git("add", "--", str(data_dir))
    if git("diff", "--cached", "--quiet", "--", str(data_dir)).returncode != 0:
        msg = f"Messwerte bis {iso(int(time.time()))}"
        res = git("commit", "-m", msg, "--", str(data_dir))
        if res.returncode != 0:
            print("--push: Commit fehlgeschlagen:", res.stderr.strip())
            return
        print("Commit:", msg)
    res = git("push")
    if res.returncode != 0:
        print("--push: Push fehlgeschlagen (wird beim naechsten Lauf nachgeholt):",
              res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "")
    elif "Everything up-to-date" not in res.stderr:
        print("Push erledigt")


# ------------------------------------------------------------------- Bluetooth

async def download_history(device, minutes: int, verbose: bool):
    """Verbindet sich und liefert (t0, rows). rows ist aufsteigend nach Zeit sortiert."""
    from bleak import BleakClient

    def log(*a):
        if verbose:
            print("   ", *a, flush=True)

    log("Verbinde ...")
    async with BleakClient(device, timeout=45) as client:
        log("Verbunden")
        chars = {c.uuid.lower(): c for s in client.services for c in s.characteristics}
        for needed in (CH_REQUEST, CH_DATA):
            if needed not in chars:
                raise RuntimeError(f"Charakteristik {needed} fehlt, anderes Geraetemodell?")
        log("Charakteristiken:", ", ".join(sorted(u[-4:] if u.startswith("494e") else u
                                                  for u in chars)))

        key = None
        if CH_AUTH_NOTIFY in chars and CH_AUTH_WRITE in chars:
            log("Geraet verlangt Verschluesselung, baue Sitzung auf")
            auth_q: asyncio.Queue = asyncio.Queue()
            await client.start_notify(CH_AUTH_NOTIFY, lambda _, d: auth_q.put_nowait(bytes(d)))
            await client.write_gatt_char(
                CH_AUTH_WRITE, encrypt_packet(with_checksum(b"\xe7\x01"), PRE_SHARED_KEY),
                response=True)
            rx1 = decrypt_packet(await asyncio.wait_for(auth_q.get(), 10), PRE_SHARED_KEY)
            log("RX1:", rx1.hex())
            if rx1[:2] != b"\xe7\x01" or not checksum_ok(rx1):
                raise RuntimeError(f"Unerwartete Antwort beim Sitzungsaufbau: {rx1.hex()}")
            key = rx1[2:18]
            await client.write_gatt_char(
                CH_AUTH_WRITE, encrypt_packet(with_checksum(b"\xe7\x02"), PRE_SHARED_KEY),
                response=True)
            rx2 = decrypt_packet(await asyncio.wait_for(auth_q.get(), 10), PRE_SHARED_KEY)
            log("RX2:", rx2.hex())
        else:
            log("Keine Verschluesselung noetig")

        q: asyncio.Queue = asyncio.Queue()

        def handler(uuid):
            return lambda _, d: q.put_nowait((uuid, bytes(d)))

        for uuid in (CH_COMMAND, CH_REQUEST, CH_DATA):
            if uuid in chars:
                await client.start_notify(uuid, handler(uuid))

        t0 = int(time.time()) // 60 * 60
        minutes = max(1, min(minutes, MAX_MINUTES))
        request = with_checksum(bytes([0x33, 0x01, minutes >> 8, minutes & 0xFF, 0x00, 0x01]))
        log(f"Fordere {minutes} Minuten an:", request.hex())
        await client.write_gatt_char(CH_REQUEST, encrypt_packet(request, key), response=True)

        rows = {}
        packets = 0
        done = False
        while not done:
            if not client.is_connected:
                print("  Verbindung abgebrochen, behalte die bisher empfangenen Werte")
                break
            try:
                uuid, raw = await asyncio.wait_for(q.get(), IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                log(f"{IDLE_TIMEOUT_S} s keine Daten mehr, beende")
                break
            packet = decrypt_packet(raw, key)
            if uuid == CH_DATA and len(packet) == 20:
                offset, decoded = decode_history_packet(packet, t0)
                packets += 1
                if packets <= 5:
                    log(f"Daten {packet.hex()}  Offset {offset}:",
                        " ".join(f"{t:.1f}/{h:.1f}" for _, t, h in decoded))
                elif packets % 500 == 0:
                    log(f"{packets} Pakete, noch {offset} Minuten")
                for ts, temp, hum in decoded:
                    rows[ts] = (temp, hum)
                if offset <= 6:
                    done = True
                elif packets % KEEPALIVE_EVERY == 0:
                    try:
                        await client.write_gatt_char(
                            CH_REQUEST, encrypt_packet(with_checksum(b"\xaa\x01"), key),
                            response=True)
                    except Exception as exc:  # Keep-Alive ist nicht kritisch
                        log("Keep-Alive fehlgeschlagen:", exc)
            else:
                log(f"Status {uuid[-4:]}: {packet.hex()}")
                if uuid == CH_REQUEST and packet[:2] == b"\xee\x01":
                    done = True
        log(f"{packets} Datenpakete empfangen")
        return t0, [(ts, *rows[ts]) for ts in sorted(rows)]


async def run(args) -> int:
    from bleak import BleakScanner

    data_dir = Path(args.data_dir)
    print(f"Suche {args.scan_seconds} s nach Sensoren ...", flush=True)
    found = await BleakScanner.discover(timeout=args.scan_seconds, return_adv=True)
    sensors = []
    for device, adv in found.values():
        name = adv.local_name or device.name or ""
        if "5179" in name and (not args.only or args.only in name):
            sensors.append((name, device, adv))
    if not sensors:
        print("Kein Sensor in Reichweite.")
        if args.push and not args.dry_run:
            git_publish(SCRIPT_DIR, data_dir)   # evtl. liegen gebliebene Commits pushen
        return 0

    battery = {}
    failures = 0
    for name, device, adv in sorted(sensors, key=lambda s: s[0]):
        reading = decode_advertisement(adv.manufacturer_data)
        if reading:
            battery[name] = reading[2]
            print(f"{name}: aktuell {reading[0]:.1f} °C, {reading[1]:.1f} %, "
                  f"Batterie {reading[2]} %, RSSI {adv.rssi} dBm")
        else:
            print(f"{name}: gefunden, RSSI {adv.rssi} dBm")

        sensor_dir = data_dir / name
        last = last_stored_time(sensor_dir) if sensor_dir.exists() else None
        now = int(time.time())
        minutes = MAX_MINUTES if last is None else (now - last) // 60 + 2
        if args.max_minutes:
            minutes = min(minutes, args.max_minutes)
        if last is not None and now - last < 120:
            print("  bereits aktuell")
            continue

        rows = None
        for attempt in range(1, args.retries + 1):
            try:
                # BlueZ vergisst Geraete kurz nach Ende einer Suche wieder. Darum vor
                # jedem Verbindungsversuch frisch suchen und sofort verbinden.
                fresh = await BleakScanner.find_device_by_address(
                    device.address, timeout=args.scan_seconds + 10)
                if fresh is None:
                    raise RuntimeError("Sensor bei erneuter Suche nicht gefunden")
                _, rows = await download_history(fresh, minutes, args.verbose)
                break
            except Exception as exc:
                detail = str(exc) or "keine Antwort innerhalb der Wartezeit"
                print(f"  Versuch {attempt}/{args.retries} fehlgeschlagen: "
                      f"{type(exc).__name__}: {detail}")
                await asyncio.sleep(2)
        if rows is None:
            failures += 1
            continue

        if last is not None:
            rows = [r for r in rows if r[0] > last]
        if not rows:
            print("  keine neuen Werte")
            continue
        print(f"  {len(rows)} neue Werte von {iso(rows[0][0])} bis {iso(rows[-1][0])}")
        if args.dry_run:
            print("  (--dry-run: nichts geschrieben)")
        else:
            append_rows(sensor_dir, rows)

    if not args.dry_run and data_dir.exists():
        write_index(data_dir, battery)
        if args.push:
            git_publish(SCRIPT_DIR, data_dir)
    return 1 if failures else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Govee-H5179-Verlauf per Bluetooth in CSV-Dateien sichern")
    ap.add_argument("--data-dir", default=str(SCRIPT_DIR / "data"),
                    help="Zielordner (Standard: data neben dem Skript)")
    ap.add_argument("--push", action="store_true",
                    help="neue Werte committen und pushen (Skript muss im Repo liegen)")
    ap.add_argument("--scan-seconds", type=float, default=15, help="Dauer der Geraetesuche")
    ap.add_argument("--max-minutes", type=int, default=0,
                    help="hoechstens so viele Minuten zurueck abholen (0 = alles Neue)")
    ap.add_argument("--only", default="", help="nur Sensoren, deren Name diesen Text enthaelt")
    ap.add_argument("--retries", type=int, default=3, help="Verbindungsversuche pro Sensor")
    ap.add_argument("--dry-run", action="store_true", help="nichts schreiben, nur anzeigen")
    ap.add_argument("-v", "--verbose", action="store_true", help="Protokolldetails ausgeben")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
