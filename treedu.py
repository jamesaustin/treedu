#!/usr/bin/env python3
"""Interactive directory size and file-count viewer with periodic refresh."""

import argparse
import collections
import curses
import os
import queue
import threading
import time
from typing import DefaultDict, Dict, List, Tuple

SORT_COLUMNS = ("name", "files", "subtree_files", "initial", "current", "delta")


def human_readable(size: int) -> str:
    """Format sizes in human-readable units."""
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.0f}{unit}"
        value /= 1024
    return f"{value:.0f}{units[-1]}"


def display_name(path: str, root: str, depth: int) -> str:
    """Return the indented label shown for a directory."""
    name = "." if path == root else os.path.basename(path.rstrip(os.sep)) or path
    indent = "  " * depth
    return f"{indent}{name}"


def sort_value(
    path: str,
    sort_column: str,
    root: str,
    initial_sizes: Dict[str, int],
    current_sizes: Dict[str, int],
    file_counts: Dict[str, int],
    total_file_counts: Dict[str, int],
    deltas: Dict[str, int],
):
    """Return the per-column sort key for a path."""
    if sort_column == "name":
        return display_name(path, root, 0).casefold()
    if sort_column == "files":
        return file_counts.get(path, 0)
    if sort_column == "subtree_files":
        return total_file_counts.get(path, 0)
    if sort_column == "initial":
        return initial_sizes.get(path, 0)
    if sort_column == "current":
        return current_sizes.get(path, 0)
    return deltas.get(path, 0)


def human_readable_count(count: int) -> str:
    """Format counts with decimal suffixes."""
    units = ["", "K", "M", "B", "T"]
    value = float(count)
    for unit in units:
        if abs(value) < 1000 or unit == units[-1]:
            if unit == "":
                return f"{int(value)}"
            if value >= 100:
                return f"{value:.0f}{unit}"
            if value >= 10:
                return f"{value:.1f}{unit}"
            return f"{value:.2f}{unit}"
        value /= 1000
    return f"{value:.0f}{units[-1]}"


def scan_directory_with_progress(
    root: str, progress_cb=None
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int], DefaultDict[str, List[str]]]:
    """Walk the tree and return aggregate sizes, file counts, and child mapping."""
    sizes: Dict[str, int] = {}
    file_counts: Dict[str, int] = {}
    total_file_counts: Dict[str, int] = {}
    children: DefaultDict[str, List[str]] = collections.defaultdict(list)

    # Pre-count directories for progress percentages.
    total_dirs = 1
    for _, dirnames, _ in os.walk(root):
        total_dirs += len(dirnames)
    processed_dirs = 0

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        total = 0
        subtree_files = len(filenames)
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                total += os.path.getsize(fpath)
            except OSError:
                # Ignore files we cannot stat.
                continue

        for dirname in dirnames:
            child_path = os.path.join(dirpath, dirname)
            total += sizes.get(child_path, 0)
            subtree_files += total_file_counts.get(child_path, 0)
            children[dirpath].append(child_path)

        children[dirpath].sort()
        sizes[dirpath] = total
        file_counts[dirpath] = len(filenames)
        total_file_counts[dirpath] = subtree_files
        processed_dirs += 1
        if progress_cb:
            progress_cb(min(1.0, processed_dirs / max(1, total_dirs)))

    if root not in children:
        children[root] = []

    return sizes, file_counts, total_file_counts, children


def build_visible(
    root: str,
    children: Dict[str, List[str]],
    expanded: set,
    initial_sizes: Dict[str, int],
    current_sizes: Dict[str, int],
    file_counts: Dict[str, int],
    total_file_counts: Dict[str, int],
    deltas: Dict[str, int],
    filter_deltas: bool,
    sort_column: str,
) -> List[Tuple[str, int]]:
    """Flatten the expanded tree into a list with depth values for rendering."""
    visible: List[Tuple[str, int]] = []
    stack: List[Tuple[str, int]] = [(root, 0)]

    while stack:
        path, depth = stack.pop()
        if not filter_deltas or deltas.get(path, 0) != 0:
            visible.append((path, depth))
        if path in expanded:
            reverse_sort = sort_column != "name"
            ordered_children = sorted(
                children.get(path, []),
                key=lambda p: (
                    sort_value(
                        p,
                        sort_column,
                        root,
                        initial_sizes,
                        current_sizes,
                        file_counts,
                        total_file_counts,
                        deltas,
                    ),
                    display_name(p, root, 0),
                ),
                reverse=reverse_sort,
            )
            for child in reversed(ordered_children):
                stack.append((child, depth + 1))

    return visible


def color_for_delta(delta: int) -> int:
    if delta > 0:
        return curses.color_pair(1) | curses.A_BOLD  # Growth
    if delta < 0:
        return curses.color_pair(2) | curses.A_BOLD  # Shrink
    return curses.A_NORMAL


def render(
    stdscr,
    visible: List[Tuple[str, int]],
    initial_sizes: Dict[str, int],
    current_sizes: Dict[str, int],
    file_counts: Dict[str, int],
    total_file_counts: Dict[str, int],
    sort_column: str,
    selected: int,
    last_scan: float,
    mode_text: str,
    next_text: str,
    root: str,
    scanning: bool,
    spinner: str,
    scroll_offset: int,
    pos_pairs: List[int],
    neg_pairs: List[int],
    scan_progress: float,
    filter_deltas: bool,
) -> None:
    def safe_add(row: int, col: int, text: str, attr: int = curses.A_NORMAL) -> None:
        try:
            stdscr.addnstr(row, col, text, max(0, stdscr.getmaxyx()[1] - col), attr)
        except curses.error:
            # Ignore rendering errors caused by tiny terminals or edge writes.
            pass

    def delta_attr(delta_value: int, max_abs: int) -> int:
        if max_abs <= 0 or delta_value == 0:
            return curses.A_NORMAL
        pairs = pos_pairs if delta_value > 0 else neg_pairs
        if not pairs:
            return curses.A_NORMAL
        ratio = min(1.0, max(0.0, abs(delta_value) / max_abs))
        idx = min(len(pairs) - 1, int(ratio * (len(pairs) - 1)))
        return curses.color_pair(pairs[idx])

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    last_scan_str = (
        "pending" if last_scan <= 0 else time.strftime("%H:%M:%S", time.localtime(last_scan))
    )

    title = f"treedu - {root}"
    safe_add(0, 0, title.ljust(width), curses.A_BOLD)
    progress_text = f"{int(scan_progress * 100):3d}%" if scanning else "   "
    status = f"Scanning {spinner} {progress_text}" if scanning else "Idle"
    safe_add(
        1,
        0,
        f"{mode_text} | Last scan: {last_scan_str} | Next: {next_text} | Status: {status}".ljust(
            width
        ),
        curses.A_NORMAL,
    )
    controls = [
        ("q", "quit"),
        ("⤧", "navigation"),
        ("s", "cycle sort"),
        ("R", "rebase subtree"),
        ("f", "filter Δ only" if not filter_deltas else "show all"),
        ("events", "auto-refresh"),
    ]
    col = 0
    row = 2
    for key_text, desc in controls:
        key_str = f"[{key_text}]"
        safe_add(row, col, key_str, curses.color_pair(3) | curses.A_BOLD)
        col += len(key_str) + 1
        safe_add(row, col, f"{desc}  ", curses.A_DIM)
        col += len(desc) + 2

    direct_count_width = 8
    total_count_width = 10
    size_width = 12
    numeric_width = direct_count_width + total_count_width + (size_width * 3)
    available_for_names = max(10, width - numeric_width)

    longest_name = 0
    for path, depth in visible:
        longest_name = max(longest_name, len(display_name(path, root, depth)))

    name_col_width = min(max(20, longest_name + 2), available_for_names)

    header_columns = [
        ("name", "Folder", name_col_width, "left"),
        ("files", "Files", direct_count_width, "right"),
        ("subtree_files", "Subtree", total_count_width, "right"),
        ("initial", "Initial", size_width, "right"),
        ("current", "Current", size_width, "right"),
        ("delta", "Delta", size_width, "right"),
    ]
    header_col = 0
    for column_key, label, column_width, align in header_columns:
        is_active = column_key == sort_column
        content_width = max(0, column_width - (1 if is_active else 0))
        if align == "left":
            header_text = label.ljust(content_width if is_active else column_width)
        else:
            header_text = label.rjust(content_width if is_active else column_width)
        safe_add(4, header_col, header_text, curses.A_UNDERLINE)
        if is_active and column_width > 0:
            safe_add(
                4,
                header_col + column_width - 1,
                "↓",
                curses.color_pair(3) | curses.A_BOLD | curses.A_UNDERLINE,
            )
        header_col += column_width

    deltas = [current_sizes.get(p, 0) - initial_sizes.get(p, 0) for p, _ in visible]
    max_abs_delta = max((abs(d) for d in deltas), default=0)

    max_rows = max(0, height - 5)
    for draw_idx, (path, depth) in enumerate(visible[scroll_offset : scroll_offset + max_rows]):
        row = 5 + draw_idx
        if row >= height:
            break

        name = display_name(path, root, depth)
        if len(name) > name_col_width:
            if name_col_width > 3:
                name = name[: name_col_width - 3] + "..."
            else:
                name = name[:name_col_width]

        initial = initial_sizes.get(path, 0)
        current = current_sizes.get(path, 0)
        direct_files = file_counts.get(path, 0)
        subtree_files = total_file_counts.get(path, 0)
        delta = current - initial
        delta_sign = "+" if delta > 0 else ""
        delta_text = f"{delta_sign}{human_readable(delta)}"

        line_prefix = (
            name.ljust(name_col_width)
            + human_readable_count(direct_files).rjust(direct_count_width)
            + human_readable_count(subtree_files).rjust(total_count_width)
            + human_readable(initial).rjust(size_width)
            + human_readable(current).rjust(size_width)
        )

        row_attr = curses.A_NORMAL
        if scroll_offset + draw_idx == selected:
            row_attr |= curses.A_REVERSE

        safe_add(row, 0, line_prefix, row_attr)
        delta_column = len(line_prefix)
        delta_attr_val = row_attr | delta_attr(delta, max_abs_delta)
        safe_add(row, delta_column, delta_text.rjust(12), delta_attr_val)

    stdscr.refresh()


def scan_worker(
    root: str,
    interval: int,
    result_queue: queue.Queue,
    stop_event: threading.Event,
    scanning_event: threading.Event,
) -> None:
    """Scan in a background thread and send results to the UI."""
    while not stop_event.is_set():
        scanning_event.set()
        result_queue.put(("progress", 0.0))
        sizes, file_counts, total_file_counts, children = scan_directory_with_progress(
            root, progress_cb=lambda pct: result_queue.put(("progress", pct))
        )
        scanning_event.clear()
        result_queue.put((time.time(), sizes, file_counts, total_file_counts, children))

        # Sleep until next interval or exit.
        stop_event.wait(interval)


def nearest_existing_dir(path: str, root: str) -> str:
    """Return the nearest existing directory for a path (or root as fallback)."""
    current = path
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current or not parent.startswith(root):
            return root
        current = parent
    return current


def coalesce_paths(paths: List[str]) -> List[str]:
    """Remove nested paths when an ancestor is already queued for scanning."""
    collapsed: List[str] = []
    for path in sorted(paths, key=len):
        if not any(path == existing or path.startswith(existing + os.sep) for existing in collapsed):
            collapsed.append(path)
    return collapsed


def integrate_subtree(
    root: str,
    target: str,
    sizes: Dict[str, int],
    file_counts: Dict[str, int],
    total_file_counts: Dict[str, int],
    children: Dict[str, List[str]],
) -> None:
    """Re-scan a subtree and merge results into the aggregated maps."""
    old_subtree_paths = [p for p in list(sizes) if p == target or p.startswith(target + os.sep)]
    old_root_size = sizes.get(target, 0)
    old_root_total_files = total_file_counts.get(target, 0)

    for p in old_subtree_paths:
        sizes.pop(p, None)
        file_counts.pop(p, None)
        total_file_counts.pop(p, None)
        children.pop(p, None)

    new_sizes, new_file_counts, new_total_file_counts, new_children = scan_directory_with_progress(
        target
    )
    sizes.update(new_sizes)
    file_counts.update(new_file_counts)
    total_file_counts.update(new_total_file_counts)
    for path, kids in new_children.items():
        children[path] = kids

    new_root_size = new_sizes.get(target, 0)
    size_delta = new_root_size - old_root_size
    total_file_delta = new_total_file_counts.get(target, 0) - old_root_total_files

    # Propagate aggregate deltas up the ancestor chain.
    parent = os.path.dirname(target)
    while parent and parent.startswith(root):
        sizes[parent] = sizes.get(parent, 0) + size_delta
        total_file_counts[parent] = total_file_counts.get(parent, 0) + total_file_delta
        if parent == root:
            break
        parent = os.path.dirname(parent)


def watch_worker(
    root: str,
    result_queue: queue.Queue,
    stop_event: threading.Event,
    scanning_event: threading.Event,
) -> None:
    """Watch filesystem events and rescan only affected subtrees."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        # Signal the UI to fall back if watchdog is unavailable.
        result_queue.put(("watchdog-missing", {}, {}, {}, {}))
        return

    dirty_paths: set = set()
    dirty_event = threading.Event()
    dirty_lock = threading.Lock()

    scanning_event.set()
    result_queue.put(("progress", 0.0))
    sizes, file_counts, total_file_counts, children = scan_directory_with_progress(
        root, progress_cb=lambda pct: result_queue.put(("progress", pct))
    )
    scanning_event.clear()
    result_queue.put((time.time(), sizes, file_counts, total_file_counts, children))

    def mark_dirty(path: str) -> None:
        existing = nearest_existing_dir(path, root)
        if existing.startswith(root):
            with dirty_lock:
                dirty_paths.add(existing)
                dirty_event.set()

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            mark_dirty(event.src_path)
            dest = getattr(event, "dest_path", None)
            if dest:
                mark_dirty(dest)

    observer = Observer()
    observer.schedule(Handler(), root, recursive=True)
    observer.start()

    try:
        while not stop_event.is_set():
            if not dirty_event.wait(timeout=0.2):
                continue

            with dirty_lock:
                targets = coalesce_paths(list(dirty_paths))
                dirty_paths.clear()
                dirty_event.clear()

            scanning_event.set()
            total = max(1, len(targets))
            for idx, target in enumerate(targets):
                result_queue.put(("progress", idx / total))
                integrate_subtree(root, target, sizes, file_counts, total_file_counts, children)
            result_queue.put(("progress", 1.0))
            scanning_event.clear()

            result_queue.put(
                (
                    time.time(),
                    dict(sizes),
                    dict(file_counts),
                    dict(total_file_counts),
                    dict(children),
                )
            )
    finally:
        observer.stop()
        observer.join(timeout=1)


def tui(stdscr, root: str, interval: int, use_watch: bool) -> None:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)

    def build_gradient_pairs() -> (List[int], List[int]):
        # Use xterm-256 color IDs to approximate a 24-bit gradient.
        pos_palette = [226, 220, 214, 208, 202, 196]  # Yellow -> Red
        neg_palette = [46, 48, 51, 39, 27, 21]  # Green -> Blue
        pos_pairs: List[int] = []
        neg_pairs: List[int] = []
        pair_id = 10

        if curses.COLORS < 16:
            return pos_pairs, neg_pairs

        for color_id in pos_palette:
            if color_id >= curses.COLORS or pair_id >= curses.COLOR_PAIRS:
                break
            curses.init_pair(pair_id, color_id, -1)
            pos_pairs.append(pair_id)
            pair_id += 1

        for color_id in neg_palette:
            if color_id >= curses.COLORS or pair_id >= curses.COLOR_PAIRS:
                break
            curses.init_pair(pair_id, color_id, -1)
            neg_pairs.append(pair_id)
            pair_id += 1

        return pos_pairs, neg_pairs

    stdscr.nodelay(True)

    # Start with empty data until the first scan completes.
    initial_sizes: Dict[str, int] = {}
    current_sizes: Dict[str, int] = {}
    file_counts: Dict[str, int] = {}
    total_file_counts: Dict[str, int] = {}
    children: Dict[str, List[str]] = {root: []}
    expanded = {root}
    selected = 0
    scroll_offset = 0
    last_scan = 0.0
    scanning_event = threading.Event()
    stop_event = threading.Event()
    result_queue: queue.Queue = queue.Queue()
    pos_pairs, neg_pairs = build_gradient_pairs()
    scan_progress = 0.0
    filter_deltas = False
    sort_index = SORT_COLUMNS.index("initial")

    worker_target = watch_worker if use_watch else scan_worker
    worker_args = (
        (root, result_queue, stop_event, scanning_event)
        if use_watch
        else (root, interval, result_queue, stop_event, scanning_event)
    )

    worker = threading.Thread(target=worker_target, args=worker_args, daemon=True)
    worker.start()

    spinner_frames = "|/-\\"
    spinner_index = 0
    mode_text = "Mode: watch (events)" if use_watch else f"Scan every {interval}s"
    next_text = "on change" if use_watch else "pending"

    while True:
        spinner = spinner_frames[spinner_index % len(spinner_frames)]
        spinner_index += 1

        # Pull latest scan result or progress if available.
        while True:
            try:
                result = result_queue.get_nowait()
            except queue.Empty:
                break

            if isinstance(result, tuple) and result and result[0] == "progress":
                scan_progress = float(result[1])
                continue

            timestamp, new_sizes, new_file_counts, new_total_file_counts, new_children = result
            if timestamp == "watchdog-missing":
                # Watchdog not installed; fall back to periodic scanning.
                use_watch = False
                mode_text = f"Scan every {interval}s (watchdog missing)"
                last_scan = 0.0
                next_text = "pending"
                worker = threading.Thread(
                    target=scan_worker,
                    args=(root, interval, result_queue, stop_event, scanning_event),
                    daemon=True,
                )
                worker.start()
            else:
                last_scan = float(timestamp)
                scan_progress = 1.0
                current_sizes = new_sizes
                file_counts = new_file_counts
                total_file_counts = new_total_file_counts
                children = new_children
                if not initial_sizes:
                    # First scan establishes the baseline.
                    initial_sizes = dict(new_sizes)
                else:
                    for path, size in new_sizes.items():
                        initial_sizes.setdefault(path, 0)

        now = time.time()
        if use_watch:
            next_text = "on change"
        elif last_scan <= 0:
            next_text = "pending"
        else:
            remaining = max(0, int(interval - (now - last_scan)))
            next_text = f"{remaining}s"

        delta_map = {p: current_sizes.get(p, 0) - initial_sizes.get(p, 0) for p in current_sizes}
        sort_column = SORT_COLUMNS[sort_index]
        visible = build_visible(
            root,
            children,
            expanded,
            initial_sizes,
            current_sizes,
            file_counts,
            total_file_counts,
            delta_map,
            filter_deltas,
            sort_column,
        )
        if selected >= len(visible):
            selected = max(0, len(visible) - 1)
        height, _ = stdscr.getmaxyx()
        max_rows = max(0, height - 5)
        scroll_offset = min(scroll_offset, max(0, len(visible) - max_rows))
        if selected < scroll_offset:
            scroll_offset = selected
        elif selected >= scroll_offset + max_rows and max_rows > 0:
            scroll_offset = selected - max_rows + 1

        render(
            stdscr,
            visible,
            initial_sizes,
            current_sizes,
            file_counts,
            total_file_counts,
            sort_column,
            selected,
            last_scan,
            mode_text,
            next_text,
            root,
            scanning_event.is_set(),
            spinner,
            scroll_offset,
            pos_pairs,
            neg_pairs,
            scan_progress,
            filter_deltas,
        )

        key = stdscr.getch()
        if key in (ord("q"), 27):
            break
        elif key in (curses.KEY_DOWN,):
            selected = min(selected + 1, len(visible) - 1)
        elif key in (curses.KEY_UP,):
            selected = max(selected - 1, 0)
        elif key in (curses.KEY_RIGHT,):
            path, _ = visible[selected]
            if children.get(path):
                expanded.add(path)
        elif key in (curses.KEY_LEFT,):
            path, _ = visible[selected]
            if path in expanded and path != root:
                expanded.discard(path)
            else:
                parent = os.path.dirname(path)
                if parent.startswith(root):
                    try:
                        parent_index = [p for p, _ in visible].index(parent)
                        selected = parent_index
                    except ValueError:
                        pass
        elif key in (ord("R"),):
            path, _ = visible[selected]
            # Re-baseline the selected subtree to its current sizes.
            stack = [path]
            while stack:
                p = stack.pop()
                initial_sizes[p] = current_sizes.get(p, 0)
                stack.extend(children.get(p, []))
            last_scan = time.time()
        elif key in (ord("f"),):
            filter_deltas = not filter_deltas
            scroll_offset = 0
            selected = 0
        elif key in (ord("s"),):
            sort_index = (sort_index + 1) % len(SORT_COLUMNS)

        time.sleep(0.05)

    stop_event.set()
    worker.join(timeout=1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Browse folder sizes and file counts with a live-updating terminal UI."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Root path to scan (default: current directory).",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=60,
        help="Seconds between rescans (default: 60).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = os.path.abspath(args.path)
    interval = max(1, args.interval)
    use_watch = True

    if not os.path.isdir(root):
        print(f"Path is not a directory: {root}")
        return 1

    curses.wrapper(tui, root, interval, use_watch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
