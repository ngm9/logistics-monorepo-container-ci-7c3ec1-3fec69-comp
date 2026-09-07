from http.server import BaseHTTPRequestHandler, HTTPServer
from shared.envelope import response
from shared.logging import request_line

SERVICE = "customer-notification"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(request_line(SERVICE, self.path), flush=True)
        if self.path in ("/", "/health"):
            body = response(SERVICE, route=self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
