// Tiny static file server, no dependencies. Railway sets PORT for you.
const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = process.env.PORT || 3000;
const ROOT = __dirname;
const FILES = {
  "/": "index.html",
  "/index.html": "index.html",
  "/commands.json": "commands.json",
};
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

http
  .createServer((req, res) => {
    const url = (req.url || "/").split("?")[0];
    const file = FILES[url];
    if (!file) {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      return res.end("Not found");
    }
    fs.readFile(path.join(ROOT, file), (err, body) => {
      if (err) {
        res.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
        return res.end("Could not read " + file);
      }
      res.writeHead(200, {
        "Content-Type": TYPES[path.extname(file)],
        "Cache-Control": file.endsWith(".json") ? "no-cache" : "public, max-age=300",
      });
      res.end(body);
    });
  })
  .listen(PORT, () => console.log("Heal site listening on " + PORT));
