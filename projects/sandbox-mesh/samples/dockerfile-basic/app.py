import http.server
import os


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"dockerfile-basic ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


port = int(os.environ.get("PORT", "8080"))
print("dockerfile-basic ok", flush=True)
http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
