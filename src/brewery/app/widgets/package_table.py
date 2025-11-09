"""Widget to display a table of packages."""

from __future__ import annotations

from textual.widgets import DataTable
from textual.message import Message
from textual.reactive import reactive

from brewery.core.models import PackageRow


class PackageTable(DataTable):
    """Widget to display a table of packages."""

    class RowSelected(Message):
        """Message sent when a row is selected."""
        def __init__(self, row_key: str) -> None:
            self.row_key = row_key
            super().__init__()

    sorted_by: reactive[str | None] = reactive(None)

    def on_mount(self) -> None:
        """Called when the widget is mounted."""
        self.zebra_stripes = True
        self.cursor_type = "row"

        self.add_column("Name", key="name")
        self.add_column("Type", key="type")
        self.add_column("Version", key="version")
        self.add_column("Status", key="status")
        self.add_column("Size", key="size")
        self.add_column("Installed", key="installed")

    def load_rows(self, rows: list[PackageRow]) -> None:
        """Load rows into the table."""
        self.clear()
        for row in rows:
            self.add_row(
                row.name,
                row.type,
                row.version,
                row.status,
                row.size_human,
                row.installed_at,
                key=row.key
            )

        if "name" in self.columns:
            self.sort("name")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection event."""
        if event.row_key is not None:
            self.post_message(self.RowSelected(str(event.row_key)))

    def key_s(self) -> None:
        """Sort by Name column."""
        self.sort("Name")