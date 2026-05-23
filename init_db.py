import os
import subprocess
import sys

def run_command(command):
    print(f"\n[+] Đang chạy: {' '.join(command)}")
    result = subprocess.run(command, text=True)
    if result.returncode != 0:
        print(f"[!] Lỗi khi chạy lệnh: {' '.join(command)}")
        sys.exit(1)

def main():
    print("=== BẮT ĐẦU KHỞI TẠO DATABASE CHO DỰ ÁN ===")
    
    # Bước 1: Tạo các file migration cho app_dashboard
    run_command([sys.executable, "manage.py", "makemigrations", "app_dashboard"])
    
    # Bước 2: Áp dụng migrations để tạo tất cả các bảng trong Database
    run_command([sys.executable, "manage.py", "migrate"])
    
    # Bước 3: Tạo tài khoản Admin (Nếu bạn đã có file create_admin.py)
    if os.path.exists("create_admin.py"):
        print("\n[+] Kiểm tra và tạo tài khoản Admin...")
        subprocess.run([sys.executable, "create_admin.py"])
    
    # Bước 4: Chạy file seed.py để nạp dữ liệu ban đầu (Domain, Keyword)
    if os.path.exists("seed.py"):
        print("\n[+] Đang nạp dữ liệu khởi tạo (seed)...")
        run_command([sys.executable, "seed.py"])

    print("\n=== KHỞI TẠO DATABASE THÀNH CÔNG! ===")

if __name__ == "__main__":
    # Chuyển working directory về thư mục chứa file script (gốc dự án)
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
