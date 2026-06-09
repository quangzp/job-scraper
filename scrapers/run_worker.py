import os
import sys
import argparse
import asyncio
import random


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"Invalid integer value for {name}={value!r}. Using default={default}.")
        return default

def setup_django():
    # Thêm thư mục gốc vào sys.path để Django có thể tìm thấy core.settings
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root_dir not in sys.path:
        sys.path.append(root_dir)
        
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
    
    import django
    django.setup()

def main():
    parser = argparse.ArgumentParser(description="Job Platform Scraper Worker")
    parser.add_argument('--mode', choices=['harvest', 'extract'], required=True, help="Chế độ chạy: harvest (thu thập link) hoặc extract (bóc tách chi tiết)")
    parser.add_argument('--domain', type=str, help="Domain cụ thể để chạy (ví dụ: topcv, vnworks)")
    parser.add_argument('--storage-dir', type=str, help="Thư mục storage riêng cho Crawlee (ví dụ: storage/harvest)")
    
    args = parser.parse_args()

    if args.storage_dir:
        os.environ['CRAWLEE_STORAGE_DIR'] = args.storage_dir
    
    # Thiết lập Django ORM trước tiên
    setup_django()
    
    # Import các models sau khi django.setup()
    from app_dashboard.models import JobLink
    
    print(f"Starting worker in mode: {args.mode}")
    
    import importlib
    
    def get_class_dynamically(module_path, base_class):
        try:
            module = importlib.import_module(module_path)
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, base_class) and attr is not base_class:
                    return attr
        except ModuleNotFoundError:
            pass
        return None

    if args.mode == 'harvest':
        async def run_harvester():
            from app_dashboard.models import TargetDomain, Keyword
            from asgiref.sync import sync_to_async
            from scrapers.harvesters.base import BaseHarvester
            from scrapers.utils.run_log import begin_domain_run, finish_domain_run, get_domain_run_usage
            
            print("Manager Harvester đã khởi động. Quét hệ thống theo chu kỳ ngẫu nhiên.")
            
            while True:
                domains = await sync_to_async(list)(
                    TargetDomain.objects.filter(is_active=True, is_harvest_enabled=True)
                )
                if args.domain:
                    domains = [d for d in domains if d.name == args.domain]
                    
                keywords = await sync_to_async(list)(Keyword.objects.filter(is_active=True))
                
                if not domains:
                    print("Không có domain nào đang active để chạy Harvester.")
                elif not keywords:
                    print("Không có keyword nào đang active để chạy Harvester.")
                else:
                    for domain in domains:
                        print(f"Bắt đầu thu thập cho domain: {domain.name}")
                        module_path = f"scrapers.harvesters.{domain.name}"
                        harvester_class = get_class_dynamically(module_path, BaseHarvester)
                        
                        if harvester_class:
                            run_info = await sync_to_async(begin_domain_run)(domain, 'HARVEST')
                            if not run_info:
                                usage = await sync_to_async(get_domain_run_usage)(domain, 'HARVEST')
                                print(
                                    f"Skip Harvester domain={domain.name}: "
                                    f"daily_runs={usage.used_runs}/{usage.configured_runs} "
                                    f"run_date={usage.run_date}."
                                )
                                continue

                            print(
                                f"Starting Harvester domain={domain.name}: "
                                f"daily_runs={run_info.run_number}/{run_info.configured_runs} "
                                f"run_date={run_info.run_date} started_at={run_info.started_at}."
                            )
                            try:
                                harvester = harvester_class(domain_config=domain)
                                for kw in keywords:
                                    await harvester.harvest(kw.name)
                            except Exception as exc:
                                await sync_to_async(finish_domain_run)(
                                    run_info.log_id,
                                    'FAILED',
                                    error_message=str(exc),
                                )
                                print(f"Harvester domain={domain.name} failed: {exc}")
                            else:
                                await sync_to_async(finish_domain_run)(run_info.log_id, 'SUCCESS')
                        else:
                            print(f"Chưa hỗ trợ Harvester (chưa có code class) cho domain: {domain.name}")
                            
                print("Hoàn thành vòng quét Harvester.")
                sleep_min = env_int('HARVEST_LOOP_SLEEP_MIN_SECONDS', 45 * 60)
                sleep_max = env_int('HARVEST_LOOP_SLEEP_MAX_SECONDS', 90 * 60)
                if sleep_min > sleep_max:
                    sleep_min, sleep_max = sleep_max, sleep_min
                sleep_seconds = random.randint(max(1, sleep_min), max(1, sleep_max))
                print(f"Next Harvester cycle starts in {sleep_seconds // 60}m {sleep_seconds % 60}s.")
                await asyncio.sleep(sleep_seconds)
                    
        asyncio.run(run_harvester())
        
    elif args.mode == 'extract':
        async def run_extractor():
            from app_dashboard.models import TargetDomain
            from asgiref.sync import sync_to_async
            from scrapers.extractors.base import BaseExtractor
            from scrapers.utils.run_log import begin_domain_run, finish_domain_run, get_domain_run_usage
            
            running_tasks = {} # domain_name -> asyncio.Task
            max_parallel_domains = max(1, env_int('EXTRACT_MAX_PARALLEL_DOMAINS', 2))
            print(f"Extractor max parallel domains: {max_parallel_domains}")

            async def run_extractor_task(extractor, batch_size, run_info):
                try:
                    items_count = await extractor.extract(batch_size=batch_size)
                except asyncio.CancelledError:
                    await sync_to_async(finish_domain_run)(
                        run_info.log_id,
                        'FAILED',
                        error_message='Extractor task cancelled.',
                    )
                    raise
                except Exception as exc:
                    await sync_to_async(finish_domain_run)(
                        run_info.log_id,
                        'FAILED',
                        error_message=str(exc),
                    )
                    print(f"Extractor domain={run_info.domain} failed: {exc}")
                else:
                    await sync_to_async(finish_domain_run)(
                        run_info.log_id,
                        'SUCCESS',
                        items_count=items_count or 0,
                    )

            print("Manager Extractor đã khởi động. Quét domain mỗi 30 phút.")
            
            while True:
                # 1. Lấy danh sách domain active hiện tại
                active_domains = await sync_to_async(list)(
                    TargetDomain.objects.filter(is_active=True, is_extract_enabled=True)
                )
                
                if args.domain:
                    active_domains = [d for d in active_domains if d.name == args.domain]
                
                active_domain_names = {d.name for d in active_domains}
                
                # 2. Dừng các task không còn active (is_active = False)
                domains_to_stop = [name for name in running_tasks.keys() if name not in active_domain_names]
                for name in domains_to_stop:
                    print(f"Domain {name} đã bị vô hiệu hóa. Đang hủy task Extractor...")
                    running_tasks[name].cancel()
                    del running_tasks[name]

                # Dọn dẹp các task đã tự kết thúc
                finished_domains = [name for name, task in running_tasks.items() if task.done()]
                for name in finished_domains:
                    print(f"Task cho domain {name} đã kết thúc.")
                    del running_tasks[name]

                # 3. Khởi động task cho domain mới
                for domain in active_domains:
                    if len(running_tasks) >= max_parallel_domains:
                        print(
                            f"Max parallel extractor domains reached ({max_parallel_domains}). "
                            "Remaining domains will wait for the next cycle."
                        )
                        break

                    if domain.name not in running_tasks:
                        print(f"Phát hiện domain mới/active: {domain.name}. Đang khởi tạo Extractor...")
                        module_path = f"scrapers.extractors.{domain.name}"
                        extractor_class = get_class_dynamically(module_path, BaseExtractor)
                        
                        if extractor_class:
                            run_info = await sync_to_async(begin_domain_run)(domain, 'EXTRACT')
                            if not run_info:
                                usage = await sync_to_async(get_domain_run_usage)(domain, 'EXTRACT')
                                print(
                                    f"Skip Extractor domain={domain.name}: "
                                    f"daily_runs={usage.used_runs}/{usage.configured_runs} "
                                    f"run_date={usage.run_date}."
                                )
                                continue

                            print(
                                f"Starting Extractor domain={domain.name}: "
                                f"daily_runs={run_info.run_number}/{run_info.configured_runs} "
                                f"run_date={run_info.run_date} started_at={run_info.started_at}."
                            )
                            extractor = extractor_class(domain_config=domain)
                            batch_size = max(1, int(getattr(domain, 'extract_batch_size', 3) or 3))
                            task = asyncio.create_task(run_extractor_task(extractor, batch_size, run_info))
                            running_tasks[domain.name] = task
                        else:
                            print(f"Chưa hỗ trợ Extractor cho domain: {domain.name}")

                # 4. Nghỉ 10 giây trước lần quét tiếp theo
                print(f"Đang quản lý {len(running_tasks)} domains. Kiểm tra lại sau 10 giây...")
                await asyncio.sleep(15)
                
        asyncio.run(run_extractor())
if __name__ == "__main__":
    main()
