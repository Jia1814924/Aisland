# -*- coding: utf-8 -*-
"""只读检测 Windows VPN 会话和第三方隧道网卡。"""
from __future__ import annotations

import ctypes as C
import ipaddress
import re
import socket
from ctypes import wintypes as W


class _RasConnection(C.Structure):
    _pack_ = 4  # ras.h 使用四字节对齐，包括 x64。
    _fields_ = [
        ("size", W.DWORD), ("handle", W.HANDLE), ("entry", W.WCHAR * 257),
        ("device_type", W.WCHAR * 17), ("device_name", W.WCHAR * 129),
        ("phonebook", W.WCHAR * 260), ("sub_entry", W.DWORD),
        ("entry_guid", C.c_byte * 16), ("flags", W.DWORD), ("luid", W.DWORD * 2),
        ("correlation_guid", C.c_byte * 16),
    ]


class _RasStatus(C.Structure):
    _pack_ = 4
    _fields_ = [
        ("size", W.DWORD), ("state", C.c_int), ("error", W.DWORD),
        ("device_type", W.WCHAR * 17), ("device_name", W.WCHAR * 129),
        ("phone", W.WCHAR * 129), ("endpoints", W.DWORD * 10), ("substate", C.c_int),
    ]


def _connected_ras() -> list[str]:
    ras = C.WinDLL("rasapi32")
    enumerate_connections = ras.RasEnumConnectionsW
    enumerate_connections.argtypes = [C.POINTER(_RasConnection), C.POINTER(W.DWORD), C.POINTER(W.DWORD)]
    enumerate_connections.restype = W.DWORD
    get_status = ras.RasGetConnectStatusW
    get_status.argtypes = [W.HANDLE, C.POINTER(_RasStatus)]
    get_status.restype = W.DWORD

    stride = C.sizeof(_RasConnection)
    size, count = W.DWORD(stride), W.DWORD()
    for _ in range(3):
        entries = (_RasConnection * max(1, (size.value + stride - 1) // stride))()
        entries[0].size = stride
        size.value = C.sizeof(entries)
        result = enumerate_connections(entries, C.byref(size), C.byref(count))
        if result != 603:  # ERROR_BUFFER_TOO_SMALL
            break
    if result or count.value > len(entries):
        return []

    names = []
    for entry in entries[:count.value]:
        if entry.device_type.casefold() != "vpn":
            continue
        status = _RasStatus(size=C.sizeof(_RasStatus))
        if get_status(entry.handle, C.byref(status)) == 0 and status.state == 0x2000 and not status.error:
            names.append(entry.entry or "Windows VPN")
    return names


class _Unicast(C.Structure):
    pass


# IP_ADAPTER_UNICAST_ADDRESS_LH 的开头，含 SOCKET_ADDRESS。
_Unicast._fields_ = [("alignment", C.c_uint64), ("next", C.POINTER(_Unicast)),
                     ("sockaddr", C.c_void_p), ("sockaddr_size", C.c_int)]


class _Adapter(C.Structure):
    pass


# IP_ADAPTER_ADDRESSES_LH 的开头。Windows 自行填充后续字段。
_Adapter._fields_ = [
    ("alignment", C.c_uint64), ("next", C.POINTER(_Adapter)), ("adapter_name", C.c_void_p),
    ("unicast", C.POINTER(_Unicast)), ("anycast", C.c_void_p), ("multicast", C.c_void_p),
    ("dns_servers", C.c_void_p), ("dns_suffix", W.LPWSTR), ("description", W.LPWSTR),
    ("friendly_name", W.LPWSTR), ("mac", W.BYTE * 8), ("mac_size", W.ULONG),
    ("flags", W.ULONG), ("mtu", W.ULONG), ("if_type", W.ULONG), ("oper_status", C.c_int),
]

_TUNNEL_DRIVERS = (
    "wireguard", "wintun", "openvpn", "tap-windows", "tailscale", "nordlynx",
    "anyconnect", "cisco secure client", "pangp virtual", "globalprotect",
    "fortinet ssl vpn", "softether vpn", "zerotier",
)


def _has_ip(current) -> bool:
    while current:
        item = current.contents
        current = item.next
        if not item.sockaddr or item.sockaddr_size < 2:
            continue
        family = C.c_ushort.from_address(item.sockaddr).value
        if family == socket.AF_INET and item.sockaddr_size >= 16:
            ip = ipaddress.IPv4Address(C.string_at(item.sockaddr + 4, 4))
        elif family == socket.AF_INET6 and item.sockaddr_size >= 28:
            ip = ipaddress.IPv6Address(C.string_at(item.sockaddr + 8, 16))
        else:
            continue
        if not (ip.is_link_local or ip.is_loopback or ip.is_unspecified or ip.is_multicast):
            return True
    return False


def _active_tunnels() -> list[str]:
    get_addresses = C.WinDLL("iphlpapi").GetAdaptersAddresses
    get_addresses.argtypes = [W.ULONG, W.ULONG, C.c_void_p, C.c_void_p, C.POINTER(W.ULONG)]
    get_addresses.restype = W.ULONG
    size = W.ULONG(15_000)
    for _ in range(3):
        buffer = C.create_string_buffer(size.value)
        result = get_addresses(socket.AF_UNSPEC, 0x000E, None, buffer, C.byref(size))
        if result != 111:  # ERROR_BUFFER_OVERFLOW
            break
    if result:
        return []

    names = []
    current = C.cast(buffer, C.POINTER(_Adapter))
    while current:
        adapter = current.contents
        current = adapter.next
        if adapter.oper_status != 1 or adapter.if_type in (24, 71):  # loopback / Wi-Fi
            continue
        name, description = adapter.friendly_name or "", adapter.description or ""
        driver = description.casefold()
        known = any(marker in driver for marker in _TUNNEL_DRIVERS)
        named_tunnel = re.fullmatch(r"(?:tun|tap|wg)\d+", name, re.IGNORECASE)
        if (known or named_tunnel) and _has_ip(adapter.unicast):
            names.append(name or description)
    return names


def connections() -> tuple[tuple[str, bool], ...]:
    """(名称, Windows 已确认连接)。第三方网卡活动仅代表本地隧道接口就绪。"""
    try:
        ras = _connected_ras()
    except OSError:
        ras = []
    try:
        tunnels = _active_tunnels()
    except OSError:
        tunnels = []
    connected = set(ras)
    return (tuple((name, True) for name in sorted(connected, key=str.casefold))
            + tuple((name, False) for name in sorted(set(tunnels) - connected, key=str.casefold)))
