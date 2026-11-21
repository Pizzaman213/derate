"""Download a repository's weights into the hub cache, and nothing else.

Run by ``prefetch.Prefetcher`` as its own process group so pause is a signal:
``python -m control_plane.deploy.prefetch_worker --repo R [--revision V]
[--pattern P ...] --cache-dir D``. It calls the same ``snapshot_download``
sparkrun calls at launch, into the same directory, so a later launch finds the
blobs complete and skips them, and a resumed prefetch picks up ``.incomplete``
files where the killed one stopped.

Exit 0 on success; on failure one ``derate-prefetch-error: <message>`` line on
stderr and exit 1, which is the only thing the parent reads.
"""

from __future__ import annotations

import argparse
import sys

ERROR_PREFIX = "derate-prefetch-error: "


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prefetch_worker")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--revision", default=None)
    parser.add_argument("--pattern", action="append", default=[])
    parser.add_argument("--cache-dir", required=True)
    args = parser.parse_args(argv)
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=args.repo,
            revision=args.revision,
            allow_patterns=args.pattern or None,
            cache_dir=args.cache_dir,
        )
    except Exception as exc:  # noqa: BLE001 -- the message is the product
        text = " ".join(str(exc).split()) or type(exc).__name__
        print(ERROR_PREFIX + text, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
