# start_kitchen.py
import multiprocessing as mp
import os
import json as j
import threading
from datetime import datetime

_order_queue = None
_manager = None
_kitchen_active_orders = None
_kitchen_lock = None          # 进程间锁（Manager().Lock()）
_log_lock = threading.Lock()  # 进程内日志锁


def init_shared_orders():
    """在主进程 __main__ 里调用一次。Queue / Manager / Lock 全部在这里创建。"""
    global _manager, _kitchen_active_orders, _order_queue, _kitchen_lock
    if _manager is None:
        _manager = mp.Manager()
        # 用 dict 存订单，key = order_no，天然去重 + del 原子
        _kitchen_active_orders = _manager.dict()
        _order_queue = mp.Queue()
        _kitchen_lock = _manager.Lock()


def get_order_queue():
    if _order_queue is None:
        raise RuntimeError("请先调用 init_shared_orders()")
    return _order_queue


def _safe_write_json(path, data):
    """原子写：先写临时文件再 os.replace，避免写一半崩溃留下半截文件。"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        j.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _append_json_log(order_data):
    log_file = '订单日志.json'
    with _log_lock:
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                logs = j.load(f)
        except (FileNotFoundError, j.JSONDecodeError):
            logs = []
        logs.append(order_data)
        _safe_write_json(log_file, logs)


def _append_txt_log(order_data):
    txt_file = '订单日志.txt'
    with _log_lock:
        with open(txt_file, 'a', encoding='utf-8') as f:
            f.write(f"\n===== Orders #{order_data['order_no']} =====\n")
            f.write(f"时间: {order_data['time']}\n")
            for num, dish in enumerate(order_data['dishes'], start=1):
                f.write(f"{num}: {dish['dish_name']} --> {dish['dish_amount']}份 "
                        f"(¥{dish['dish_price']} x {dish['dish_amount']})\n")
            f.write(f"合计: ¥{order_data['total_price']}\n")


def _handle_order(order_package):
    order_no = order_package['order_no']
    dish_list = order_package['dishes']

    total = sum(d['dish_price'] * d['dish_amount'] for d in dish_list)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    order_data = {
        'order_no': order_no,
        'time': now_str,
        'dishes': dish_list,
        'total_price': total,
        'status': 'pending'
    }

    print(f"\n========== 后厨小票 #{order_no} ==========")
    for num, d in enumerate(dish_list, start=1):
        sub = d['dish_price'] * d['dish_amount']
        print(f"{num}. {d['dish_name']}  ¥{d['dish_price']} x {d['dish_amount']} = ¥{sub}")
    print(f"合计: ¥{total}")
    print("==============================\n")

    _append_json_log(order_data)
    _append_txt_log(order_data)

    # dict 直接按 key 写，重复 order_no 会覆盖（不会出现两份）
    _kitchen_active_orders[order_no] = order_data


def kitchen_worker(q, active_orders):
    # 注意：spawn 模式下这里拿到的 active_orders 是 Manager 代理，跨进程可见
    global _kitchen_active_orders
    _kitchen_active_orders = active_orders
    print(f"[后厨进程 PID={os.getpid()}] 已启动，等待订单...")
    while True:
        package = q.get()
        if package is None:
            break
        try:
            _handle_order(package)
        except Exception as e:
            print(f"[后厨进程] 处理订单出错: {e}")


def start_kitchen():
    init_shared_orders()
    q = get_order_queue()
    p = mp.Process(
        target=kitchen_worker,
        args=(q, _kitchen_active_orders),
        daemon=True
    )
    p.start()
    return p


def get_kitchen_orders():
    if _kitchen_active_orders is None:
        return []
    # dict.values() 顺序不保证，按时间排序保证看板稳定
    orders = list(_kitchen_active_orders.values())
    orders.sort(key=lambda o: o.get('time', ''))
    return orders


def complete_order(order_no):
    """用 dict.pop 原子删除；重复点击第二个请求得到 None → 返回 False，不会错删别的单。"""
    if _kitchen_active_orders is None:
        return False
    removed = _kitchen_active_orders.pop(str(order_no), None)
    return removed is not None