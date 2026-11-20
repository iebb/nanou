"""实验性 UU IPv4 Layer-3 TUN 隧道。

子包结构：
    uu_tunnel.api      —— UU API 签名、登录与房间流程
    uu_tunnel.auth     —— 登录态持久化
    uu_tunnel.signal   —— Socket.IO 信令适配
    uu_tunnel.webrtc   —— control/SOAC 与 DataChannel 协商
    uu_tunnel.tun      —— 原生 TUN 与原始 IPv4 桥接
"""
