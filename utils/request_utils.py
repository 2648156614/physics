import re

from flask import request


def get_client_ip():
    """获取客户端 IP，适配反向代理场景。"""
    forwarded_for = request.headers.get('X-Forwarded-For', '')
    if forwarded_for:
        real_ip = forwarded_for.split(',')[0].strip()
        if real_ip:
            return real_ip[:45]

    real_ip = (request.headers.get('X-Real-IP') or '').strip()
    if real_ip:
        return real_ip[:45]

    return (request.remote_addr or '')[:45]


def is_mobile_device():
    """检测是否为移动设备。"""
    user_agent = request.headers.get('User-Agent', '').lower()
    mobile_pattern = re.compile(r'mobile|android|webos|iphone|ipad|ipod|blackberry|windows phone')
    return bool(mobile_pattern.search(user_agent))


def is_touch_device():
    """检测是否为触摸设备（简化版）。"""
    user_agent = request.headers.get('User-Agent', '').lower()
    touch_pattern = re.compile(r'mobile|android|iphone|ipad|ipod')
    return bool(touch_pattern.search(user_agent))
