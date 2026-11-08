# server.py
from flask import Flask, request, jsonify, render_template
import json
import threading
import uuid
import os
from datetime import datetime
from start_kitchen import (
    get_order_queue, start_kitchen, get_kitchen_orders, complete_order
)

app = Flask(__name__)
file_lock = threading.Lock()

MENU_FILE = '菜品数据.json'


def _read_menu_unlocked():
    try:
        with open(MENU_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def read_menu():
    with file_lock:
        return _read_menu_unlocked()


def _save_menu_unlocked(data):
    tmp = MENU_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MENU_FILE)


def save_menu(data):
    with file_lock:
        _save_menu_unlocked(data)


def mutate_menu(fn):
    """原子执行：读 → fn(menu) 就地修改 → 写。整个过程持锁，防并发丢数据。"""
    with file_lock:
        menu = _read_menu_unlocked()
        result = fn(menu)
        _save_menu_unlocked(menu)
        return result


# ---------------- 页面 ----------------
@app.route('/order')
def page_order():
    return render_template('order.html')


@app.route('/kitchen')
def page_kitchen():
    return render_template('kitchen.html')


@app.route('/admin')
def page_admin():
    return render_template('admin.html')


# ---------------- 菜单 ----------------
@app.route('/api/menu', methods=['GET'])
def api_menu():
    return jsonify(read_menu())


# ---------------- 下单 ----------------
def _generate_order_no(active_orders):
    """生成 6 位订单号，避开当前活跃订单里的号。"""
    existing = set(active_orders) if active_orders else set()
    for _ in range(50):
        candidate = str(uuid.uuid4()).replace('-', '')[:6].upper()
        if candidate not in existing:
            return candidate
    # 极端情况退化为带日期前缀的长号
    return datetime.now().strftime('%m%d%H%M%S') + str(uuid.uuid4())[:3].upper()


@app.route('/api/order', methods=['POST'])
def api_order():
    data = request.get_json(silent=True)
    if not data or not isinstance(data.get('dishes'), list):
        return jsonify({'status': 'error', 'msg': '无效请求'}), 400

    menu = read_menu()
    menu_map = {d['dish_name']: d['dish_price'] for d in menu}

    cleaned = []
    for item in data['dishes']:
        if not isinstance(item, dict):
            continue
        name = item.get('dish_name')
        amount = item.get('dish_amount', 0)
        # 校验：菜在菜单里、数量是 1~10 的整数
        if (
            name in menu_map
            and isinstance(amount, int)
            and 0 < amount <= 10
        ):
            cleaned.append({
                'dish_name': name,
                'dish_price': menu_map[name],  # 永远用服务端价，防篡改
                'dish_amount': amount
            })

    if not cleaned:
        return jsonify({'status': 'error', 'msg': '无有效菜品'}), 400

    active = get_kitchen_orders()
    order_no = _generate_order_no([o['order_no'] for o in active])

    order_package = {'order_no': order_no, 'dishes': cleaned}
    get_order_queue().put(order_package)

    return jsonify({'status': 'ok', 'order_no': order_no})


# ---------------- 后厨 ----------------
@app.route('/api/kitchen/orders', methods=['GET'])
def api_kitchen_orders():
    return jsonify(get_kitchen_orders())


@app.route('/api/kitchen/complete', methods=['POST'])
def api_kitchen_complete():
    try:
        data = request.get_json(silent=True)
        if not data or 'order_no' not in data:
            return jsonify({'status': 'error', 'msg': 'missing order_no'}), 400

        order_no = str(data['order_no'])
        if complete_order(order_no):
            print(f"[DEBUG] 订单 #{order_no} 已从看板移除")
            return jsonify({'status': 'ok'})
        else:
            print(f"[DEBUG] 找不到订单 #{order_no}（可能已被其他厨师完成）")
            return jsonify({'status': 'error', 'msg': 'order not found'}), 404
    except Exception as e:
        print(f"[DEBUG] 服务器异常: {e}")
        return jsonify({'status': 'error', 'msg': str(e)}), 500


# ---------------- 管理 ----------------
@app.route('/api/admin/add', methods=['POST'])
def api_admin_add():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'status': 'error', 'msg': '无效请求'}), 400

    name = str(data.get('name', '')).strip()
    raw_price = data.get('price')

    if not name:
        return jsonify({'status': 'error', 'msg': '菜名不能为空'}), 400

    try:
        price = int(raw_price)
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'msg': '价格必须是整数'}), 400
    if price <= 0:
        return jsonify({'status': 'error', 'msg': '价格必须大于 0'}), 400

    def _add(menu):
        if any(d['dish_name'] == name for d in menu):
            return 'duplicate'
        menu.append({'dish_name': name, 'dish_price': price})
        return 'ok'

    result = mutate_menu(_add)
    if result == 'duplicate':
        return jsonify({'status': 'error', 'msg': '菜品已存在'}), 409
    return jsonify({'status': 'ok'})


@app.route('/api/admin/delete', methods=['POST'])
def api_admin_delete():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'status': 'error', 'msg': '无效请求'}), 400

    name = str(data.get('name', '')).strip()
    if not name:
        return jsonify({'status': 'error', 'msg': '菜名不能为空'}), 400

    def _delete(menu):
        before = len(menu)
        menu[:] = [d for d in menu if d['dish_name'] != name]
        return before != len(menu)

    removed = mutate_menu(_delete)
    if not removed:
        return jsonify({'status': 'error', 'msg': '菜品不存在'}), 404
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    start_kitchen()
    # 生产环境请换：gunicorn -w 4 -k gevent server:app
    app.run(host='0.0.0.0', port=5000, threaded=True)