import os
import sys

def setup_django():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
    import django
    django.setup()

if __name__ == "__main__":
    setup_django()
    from app_dashboard.models import TargetDomain, Keyword
    
    print("Seeding initial TargetDomain...")
    topcv, _ = TargetDomain.objects.get_or_create(name='topcv', defaults={'is_active': True})
    vnworks, _ = TargetDomain.objects.get_or_create(name='vnworks', defaults={'is_active': False})
    linkedin, _ = TargetDomain.objects.get_or_create(
        name='linkedin',
        defaults={
            'is_active': False,
            'is_extract_enabled': False,
            'search_locations': ['Vietnam'],
        },
    )
    print(f"TopCV active: {topcv.is_active}, VNWorks active: {vnworks.is_active}, LinkedIn active: {linkedin.is_active}")
    
    print("Seeding initial Keyword...")
    kw, _ = Keyword.objects.get_or_create(name='python developer', defaults={'is_active': True})
    print(f"Keyword active: {kw.is_active}")
    print("Done seeding!")
