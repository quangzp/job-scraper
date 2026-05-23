import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.contrib.auth.models import User

def create_superuser():
    if not User.objects.filter(username='admin').exists():
        User.objects.create_superuser('admin', 'admin@example.com', 'admin')
        print("Đã tạo tài khoản Admin thành công!")
        print("Username: admin")
        print("Password: admin")
    else:
        print("Tài khoản admin đã tồn tại.")

if __name__ == '__main__':
    create_superuser()
