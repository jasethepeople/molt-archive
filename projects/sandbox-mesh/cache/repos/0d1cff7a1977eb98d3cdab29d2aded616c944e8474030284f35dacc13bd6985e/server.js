const http = require("node:http");
const port = Number(process.env.PORT || 8080);
const server = http.createServer((request, response) => {
  response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
  response.end("sandbox-mesh node-basic: ok\n");
});
server.listen(port, "0.0.0.0", () => console.log("node-basic ok"));
