"""Code Review 019 cycle 11: the depth model in a process of its own.

`python -m wingman.depth_worker --model NAME --threads N --input-px PX` reads
pictures on stdin and writes relative depth on stdout, in the protocol below.
A process of its own: torch's thread pool is not shared with the OCR's, a slow
first load (the model is downloaded once, about 100 MB) holds nothing up, and a
crash costs only the depth view. `--fake` answers with a made-up depth, without
torch, for tests and for checking the plumbing.

Protocol, little-endian. Request: `REQUEST` (picture_ts, height, width), then
height x width x 3 bytes of RGB. Response: `RESPONSE` (picture_ts, status,
cost_ms, height, width), then for OK height x width float16 values (larger is
nearer), for ERROR `height` bytes of UTF-8 text. READY, with no payload, says
the model is loaded. Closing stdin stops the worker.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

import numpy as np

REQUEST = struct.Struct("<dII")
RESPONSE = struct.Struct("<dBfII")
OK, READY, ERROR = 0, 1, 2


def read_exact(stream, n: int) -> "bytes | None":
    """`n` bytes from `stream`, or None at the end of it."""
    chunks, got = [], 0
    while got < n:
        chunk = stream.read(n - got)
        if not chunk:
            return None
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _send(out, picture_ts: float, status: int, cost_ms: float = 0.0,
          payload: bytes = b"", shape: "tuple[int, int]" = (0, 0)) -> None:
    out.write(RESPONSE.pack(picture_ts, status, cost_ms, shape[0], shape[1]))
    if payload:
        out.write(payload)
    out.flush()


def _send_error(out, picture_ts: float, text: str) -> None:
    data = text.encode("utf-8", "replace")[:2000]
    _send(out, picture_ts, ERROR, payload=data, shape=(len(data), 0))


def _fake_model(input_px: int):
    """Near at the bottom of the picture, far at the top: the shape of flying
    level over flat ground."""
    def infer(rgb):
        h = input_px
        w = max(1, int(round(rgb.shape[1] * input_px / max(1, rgb.shape[0]))))
        return np.repeat(np.linspace(0.0, 10.0, h, dtype=np.float32)[:, None], w, axis=1)
    return infer


def _real_model(name: str, threads: int, input_px: int):
    import torch
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation
    torch.set_num_threads(max(1, threads))
    proc = AutoImageProcessor.from_pretrained(name)
    model = AutoModelForDepthEstimation.from_pretrained(name).eval()
    size = {"height": input_px, "width": input_px}

    def infer(rgb):
        inputs = proc(images=rgb, return_tensors="pt", size=size, do_resize=True,
                      keep_aspect_ratio=True, ensure_multiple_of=14)
        with torch.inference_mode():
            return model(**inputs).predicted_depth[0].float().numpy()
    return infer


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default="depth-anything/Depth-Anything-V2-Small-hf")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--input-px", type=int, default=252)
    ap.add_argument("--fake", action="store_true")
    args = ap.parse_args(argv)
    out = sys.stdout.buffer
    try:
        infer = (_fake_model(args.input_px) if args.fake
                 else _real_model(args.model, args.threads, args.input_px))
    except Exception as exc:          # no model, no network, no torch: said once
        _send_error(out, 0.0, "load: %s: %s" % (type(exc).__name__, exc))
        return 1
    _send(out, 0.0, READY)
    inp = sys.stdin.buffer
    while True:
        head = read_exact(inp, REQUEST.size)
        if head is None:
            return 0
        picture_ts, h, w = REQUEST.unpack(head)
        raw = read_exact(inp, h * w * 3)
        if raw is None:
            return 0
        try:
            t0 = time.perf_counter()
            depth = infer(np.frombuffer(raw, np.uint8).reshape(h, w, 3))
            cost_ms = (time.perf_counter() - t0) * 1000.0
            data = np.ascontiguousarray(depth, dtype=np.float16)
            _send(out, picture_ts, OK, cost_ms, data.tobytes(), data.shape)
        except Exception as exc:
            _send_error(out, picture_ts, "%s: %s" % (type(exc).__name__, exc))


if __name__ == "__main__":
    sys.exit(main())
