import argparse
import sys
from pathlib import Path


def cmd_preprocess(args):
    from .preprocess import run_preprocess

    sources = [Path(s) for s in args.sources]
    output_dir = Path(args.output_dir)
    run_preprocess(sources, output_dir, batch_size=args.batch_size, colormap=args.colormap)


def cmd_review(args):
    import pygame as pg

    from .controller import ReviewSession

    pg.init()
    try:
        session = ReviewSession(
            work_dir=Path(args.work_dir),
            mode=args.mode,
            pass_number=args.pass_number,
            batch=args.batch,
        )
        session.run()
    finally:
        pg.quit()


def cmd_status(args):
    from .controller import load_manifest
    from .review_db import ReviewDB

    work_dir = Path(args.work_dir)
    manifest_path = work_dir / "manifest.tsv"
    if not manifest_path.exists():
        print(f"No manifest found at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest(work_dir)

    db = ReviewDB(work_dir)

    # Overall summary
    counts = db.summary(manifest)
    print(f"\nOverall: {counts['total']} images")
    print(f"  CLEAN:      {counts['CLEAN']:>6}")
    print(f"  DIRTY:      {counts['DIRTY']:>6}")
    print(f"  UNREVIEWED: {counts['UNREVIEWED']:>6}")

    # Per-batch summary
    batch_counts = db.batch_summary(manifest)
    if len(batch_counts) > 1:
        print(f"\n{'Batch':<15} {'Total':>6} {'Clean':>6} {'Dirty':>6} {'Unrev':>6}")
        print("-" * 45)
        for batch_id in sorted(batch_counts):
            bc = batch_counts[batch_id]
            print(f"{batch_id:<15} {bc['total']:>6} {bc['CLEAN']:>6} {bc['DIRTY']:>6} {bc['UNREVIEWED']:>6}")

    # Current pass
    current = db.current_pass(manifest)
    print(f"\nNext pass: {current}")


def main():
    parser = argparse.ArgumentParser(prog="image-review", description="DICOM image review for burned-in PHI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # preprocess
    p_pre = subparsers.add_parser("preprocess", help="Preprocess images for review")
    p_pre.add_argument("sources", nargs="+", help="ZIP files, directories, or image files")
    p_pre.add_argument("--batch-size", type=int, default=300, help="Images per batch (default: 300)")
    p_pre.add_argument("--output-dir", default="./review_work", help="Work directory (default: ./review_work)")
    p_pre.add_argument("--colormap", default="inferno", help="Matplotlib colormap (default: inferno)")
    p_pre.set_defaults(func=cmd_preprocess)

    # review
    p_rev = subparsers.add_parser("review", help="Review images interactively")
    p_rev.add_argument("--mode", choices=["single", "grid"], default="single", help="Review mode (default: single)")
    p_rev.add_argument("--pass", dest="pass_number", type=int, default=None, help="Pass number (auto-detected if omitted)")
    p_rev.add_argument("--batch", default=None, help="Restrict to a specific batch")
    p_rev.add_argument("--work-dir", default="./review_work", help="Work directory (default: ./review_work)")
    p_rev.set_defaults(func=cmd_review)

    # status
    p_stat = subparsers.add_parser("status", help="Show review progress")
    p_stat.add_argument("--work-dir", default="./review_work", help="Work directory (default: ./review_work)")
    p_stat.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
