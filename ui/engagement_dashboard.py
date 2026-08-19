# A simple, glanceable weekly view of the engagement data Monitor
# Lockdown has ALWAYS been collecting via its periodic AI judgment (see
# lockdown_window.py's _run_engagement_judgment()) but never surfaced
# anywhere -- previously it lived only in memory for one session's
# duration, driving the pacing bar, then discarded once lockdown closed.
# Reads core/engagement_log.py's local history -- no Notion, no LLM
# call, purely local arithmetic over sessions already logged by
# main_window.py's _close_lockdown().

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QMessageBox

import engagement_log
from theme import Card


def _format_minutes(seconds):
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"


class EngagementDashboardWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        outer.addWidget(card)

        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("<b>Last 7 days</b>"))
        header_row.addStretch()
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh)
        header_row.addWidget(refresh_button)
        reset_button = QPushButton("Reset Progress")
        reset_button.clicked.connect(self._on_reset_clicked)
        header_row.addWidget(reset_button)
        layout.addLayout(header_row)

        self.totals_label = QLabel("")
        self.totals_label.setWordWrap(True)
        layout.addWidget(self.totals_label)

        layout.addWidget(QLabel("<b>By course</b> (worst distraction rate first)"))
        self.per_course_layout = QVBoxLayout()
        self.per_course_layout.setSpacing(8)
        layout.addLayout(self.per_course_layout)

        layout.addStretch()
        self.refresh()

    def refresh(self):
        summary = engagement_log.weekly_summary()
        self._render_totals(summary)
        self._render_per_course(summary)

    def _on_reset_clicked(self):
        confirmed = QMessageBox.question(
            self, "Reset Progress",
            "Clear the WHOLE local engagement history -- every session's distraction/study "
            "time on record, not just the last 7 days shown here?\n\n"
            "This is local-only data (see this dashboard's own docstring) -- nothing in your "
            "Notion workspace is touched. There's no undo once this runs.",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        engagement_log.clear_log()
        self.refresh()

    def _render_totals(self, summary):
        if summary["total_sessions"] == 0:
            self.totals_label.setText("No lockdown sessions logged in the last 7 days yet.")
            return
        studied = _format_minutes(summary["total_studied_seconds"])
        rate_pct = round(summary["distraction_rate"] * 100)
        self.totals_label.setText(
            f"{summary['total_sessions']} session(s), {studied} studied, "
            f"{summary['total_distraction_events']} distraction event(s) "
            f"({rate_pct}% of studied time)."
        )

    def _clear_per_course_layout(self):
        while self.per_course_layout.count():
            item = self.per_course_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _render_per_course(self, summary):
        self._clear_per_course_layout()
        per_course = summary["per_course"]
        if not per_course:
            self.per_course_layout.addWidget(QLabel("Nothing to break down yet."))
            return

        def _course_rate(bucket):
            return (bucket["distracted_seconds"] / bucket["studied_seconds"]) if bucket["studied_seconds"] > 0 else 0.0

        # Worst (highest distraction rate) first -- the whole point of a
        # per-course view is spotting where focus is actually slipping,
        # not just listing courses alphabetically.
        for course, bucket in sorted(per_course.items(), key=lambda kv: _course_rate(kv[1]), reverse=True):
            rate_pct = round(_course_rate(bucket) * 100)
            studied = _format_minutes(bucket["studied_seconds"])
            row_card = Card()
            row_layout = QVBoxLayout(row_card)
            row_layout.setContentsMargins(12, 8, 12, 8)
            label = QLabel(
                f"{course}: {bucket['sessions']} session(s), {studied} studied, "
                f"{bucket['distraction_events']} distraction event(s) ({rate_pct}%)"
            )
            label.setWordWrap(True)
            row_layout.addWidget(label)
            self.per_course_layout.addWidget(row_card)
