"""HTTP 监听使用内核返回的实际绑定地址；启动不等待反向 DNS。"""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class BoundAddressMixin:
    def server_bind(self):
        # HTTPServer 为展示名反查 PTR；服务就绪、认证和路由都不需要该名字。
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class BoundThreadingHTTPServer(BoundAddressMixin, ThreadingHTTPServer):
    """沿用标准库的线程、监听与关闭语义。"""
