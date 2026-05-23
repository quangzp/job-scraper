import os
import shutil
import subprocess
import sys
import time


def main():
    # Clean Crawlee runtime storage only. Keep saved login sessions.
    storage_path = os.path.join(os.getcwd(), 'storage')
    harvester_storage = os.path.join(storage_path, 'harvest')
    extractor_storage = os.path.join(storage_path, 'extract')
    for temp_storage in (harvester_storage, extractor_storage):
        if os.path.exists(temp_storage):
            print(f"[*] Cleaning temporary storage: {temp_storage}")
            shutil.rmtree(temp_storage, ignore_errors=True)

    print("=" * 60)
    print(" STARTING ENTERPRISE JOB SCRAPER SYSTEM (ONE RUN) ")
    print("=" * 60)

    print("[1] Starting Django Web Dashboard...")
    web_process = subprocess.Popen([sys.executable, "manage.py", "runserver", "127.0.0.1:8000"])

    time.sleep(3)

    print("[2] Starting Harvester Worker...")
    harvester_process = subprocess.Popen(
        [
            sys.executable,
            "scrapers/run_worker.py",
            "--mode",
            "harvest",
            "--storage-dir",
            harvester_storage,
        ]
    )

    print("[3] Starting Extractor Worker...")
    extractor_process = subprocess.Popen(
        [
            sys.executable,
            "scrapers/run_worker.py",
            "--mode",
            "extract",
            "--storage-dir",
            extractor_storage,
        ]
    )

    try:
        web_process.wait()
        harvester_process.wait()
        extractor_process.wait()
    except KeyboardInterrupt:
        print("\n[!] Stopping all processes...")
        web_process.terminate()
        harvester_process.terminate()
        extractor_process.terminate()
        print("Stopped safely.")


if __name__ == "__main__":
    main()
