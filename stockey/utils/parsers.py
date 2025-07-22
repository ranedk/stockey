from bs4 import BeautifulSoup


def table_to_grid(table_html):
    soup = BeautifulSoup(str(table_html), "lxml")
    table = soup.find("table")

    grid = []  # Final 2-D matrix
    open_rowspans = {}  # col_idx → [rows_left, value]

    for row in table.find_all("tr"):
        row_cells = []  # cells we’ll build for this output row
        col_idx = 0

        # ─── 1. First, lay down cells that are continuing rowspans ──────────
        while col_idx in open_rowspans:
            rows_left, val = open_rowspans[col_idx]
            row_cells.append(val)
            rows_left -= 1
            if rows_left:
                open_rowspans[col_idx] = [rows_left, val]
            else:
                del open_rowspans[col_idx]
            col_idx += 1

        # ─── 2. Now process the <td>/<th> elements actually present in <tr> ──
        for cell in row.find_all(["td", "th"]):
            # Skip forward if current col_idx is still occupied by a rowspan
            while col_idx in open_rowspans:
                rows_left, val = open_rowspans[col_idx]
                row_cells.append(val)
                rows_left -= 1
                if rows_left:
                    open_rowspans[col_idx] = [rows_left, val]
                else:
                    del open_rowspans[col_idx]
                col_idx += 1

            colspan = int(cell.get("colspan", 1))
            rowspan = int(cell.get("rowspan", 1))
            value = cell.get_text(strip=True)

            # Fill current row
            for _ in range(colspan):
                row_cells.append(value)
                # If rowspan extends downward, register it
                if rowspan > 1:
                    open_rowspans[col_idx] = [rowspan - 1, value]
                col_idx += 1

        # ─── 3. At row end, we may still need to pad rowspans still open ────
        while col_idx in open_rowspans:
            rows_left, val = open_rowspans[col_idx]
            row_cells.append(val)
            rows_left -= 1
            if rows_left:
                open_rowspans[col_idx] = [rows_left, val]
            else:
                del open_rowspans[col_idx]
            col_idx += 1

        grid.append(row_cells)

    max_len = max(len(r) for r in grid)
    for r in grid:
        r.extend([None] * (max_len - len(r)))
    return grid
