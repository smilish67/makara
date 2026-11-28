"""Embedding-only transport used in the private Honcho network and during reindexing."""
import json
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MODEL = "qwen3-embedding-8b"
ENDPOINT = "https://opencode.ai/inference/go/openai/v1/embeddings"
QUERY = "Instruct: Given a question about past project decisions or personal plans, retrieve the note that answers the question.\nQuery: "
SECRET = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", re.S)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def embed(texts, key):
    payload = {"model":MODEL, "input":[SECRET.sub("[redacted]", t) for t in texts], "dimensions":768, "encoding_format":"float"}
    request = Request(ENDPOINT, json.dumps(payload).encode(), headers={
        "Authorization":"Bearer " + key, "Content-Type":"application/json", "Accept":"application/json",
        "User-Agent":"Makara-Honcho/1.0"})
    with build_opener(NoRedirect()).open(request, timeout=120) as response:
        data = json.load(response)
    items = sorted(data["data"], key=lambda item:item["index"])
    if ([item["index"] for item in items] != list(range(len(texts)))
        or any(len(item["embedding"]) != 768 or not all(math.isfinite(v) for v in item["embedding"]) for item in items)):
        raise ValueError("Invalid embedding response")
    data["data"] = items
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/embeddings":
            return self.send_error(404)
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size < 2_000_000:
                return self.send_error(413)
            body = json.loads(self.rfile.read(size))
            texts = body.get("input")
            if body.get("model") != MODEL or not isinstance(texts,list) or not 0 < len(texts) <= 128 or not all(isinstance(t,str) for t in texts):
                return self.send_error(400)
            result = embed(texts, Path("/run/secrets/go-key").read_text().strip())
            raw = json.dumps(result, allow_nan=False).encode()
        except HTTPError as exc:
            return self.send_error(exc.code)
        except (ValueError, TypeError, OSError, KeyError):
            return self.send_error(502)
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    ThreadingHTTPServer(("",8080),Handler).serve_forever()
