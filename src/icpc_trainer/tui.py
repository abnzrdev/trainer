from __future__ import annotations

import asyncio
import re
from pathlib import Path
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TypedDict

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Markdown, Static, TabPane, TabbedContent

from sqlalchemy import select

from icpc_trainer.fsrs_logic import FSRSReviewState, calculate_next_review
from icpc_trainer.models import (
    Attempt,
    AttemptStatus,
    FSRSState,
    Problem,
    create_async_sqlite_engine,
    create_session_maker,
    init_db,
)
from icpc_trainer.scraper import VJudgeScraper
from icpc_trainer.workflow import WorkflowManager


class ProblemData(TypedDict):
    id: int
    title: str
    contest_id: str
    status: str
    description: str


class WeekData(TypedDict):
    week: str
    problems: list[ProblemData]


class TrainerData(TypedDict):
    weeks: list[WeekData]


@dataclass(slots=True)
class ProblemWorkspaceState:
    solution_path: Path
    started_at: datetime


class TestResultModal(ModalScreen[None]):
    CSS = """
    TestResultModal {
        align: center middle;
    }

    #result-container {
        width: 80;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1;
    }

    #result-body {
        height: 1fr;
        overflow-y: auto;
        margin-bottom: 1;
    }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Container(id="result-container"):
            yield Static(self.title, classes="pane-title")
            yield Markdown(self.body, id="result-body")
            yield Button("Close", id="close-result", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-result":
            self.dismiss(None)


class DifficultyModal(ModalScreen[int | None]):
    CSS = """
    DifficultyModal {
        align: center middle;
    }

    #difficulty-container {
        width: 60;
        border: round $accent;
        background: $surface;
        padding: 1;
    }

    #difficulty-input {
        margin: 1 0;
    }

    #difficulty-hint {
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="difficulty-container"):
            yield Static("Perceived Difficulty", classes="pane-title")
            yield Static("Enter 1-4 (1=Again, 2=Hard, 3=Good, 4=Easy)", id="difficulty-hint")
            yield Input(placeholder="3", id="difficulty-input")
            with Horizontal():
                yield Button("Submit", id="difficulty-submit", variant="primary")
                yield Button("Skip", id="difficulty-skip", variant="default")

    def on_mount(self) -> None:
        self.query_one("#difficulty-input", Input).focus()

    def _submit(self) -> None:
        raw_value = self.query_one("#difficulty-input", Input).value.strip()
        if raw_value in {"1", "2", "3", "4"}:
            self.dismiss(int(raw_value))
            return
        self.app.bell()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "difficulty-input":
            self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "difficulty-submit":
            self._submit()
            return
        if event.button.id == "difficulty-skip":
            self.dismiss(None)


class ICPCTrainerApp(App):
    BINDINGS = [
        Binding("enter", "open_problem", "Open problem"),
        Binding("r", "run_tests", "Run tests"),
    ]

    CSS = """
    Screen {
        background: $surface;
    }

    #root {
        height: 100%;
        width: 100%;
    }

    #sidebar {
        width: 26;
        min-width: 20;
        border: round $primary;
        padding: 1;
    }

    #problem-pane {
        width: 34;
        min-width: 28;
        border: round $accent;
        padding: 1;
    }

    #detail-pane {
        border: round $success;
        padding: 1;
    }

    .pane-title {
        text-style: bold;
        margin-bottom: 1;
    }

    .list-scroll {
        height: 1fr;
    }

    .list-item {
        width: 100%;
        margin-bottom: 1;
    }

    .is-selected {
        background: $boost;
    }

    #detail-markdown {
        height: 1fr;
        width: 100%;
    }

    #daily-review-list {
        height: 1fr;
        border: round $secondary;
        padding: 1;
    }

    #daily-review-detail {
        height: 12;
        border: round $success;
        padding: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.data: TrainerData = {"weeks": []}
        self.engine = create_async_sqlite_engine()
        self.session_maker = create_session_maker(self.engine)
        self.workflow = WorkflowManager()
        self.selected_week_index: int = 0
        self.selected_problem_index: int = 0
        self.selected_daily_problem_id: int | None = None
        self.active_problem_workspaces: dict[int, ProblemWorkspaceState] = {}

    def compose(self) -> ComposeResult:
        with TabbedContent():
            with TabPane("Practice", id="practice-tab"):
                with Horizontal(id="root"):
                    with Container(id="sidebar"):
                        yield Static("Contests", classes="pane-title")
                        yield VerticalScroll(id="weekly-list", classes="list-scroll")

                    with Container(id="problem-pane"):
                        yield Static("Problems", classes="pane-title")
                        yield VerticalScroll(id="problem-list", classes="list-scroll")

                    with Container(id="detail-pane"):
                        yield Static("Problem Details", classes="pane-title")
                        yield Markdown("Select a contest and a problem to view details.", id="detail-markdown")

            with TabPane("Daily Review", id="daily-review-tab"):
                with Vertical():
                    yield Static("Problems due for review", classes="pane-title")
                    yield VerticalScroll(id="daily-review-list")
                    yield Markdown("No review item selected.", id="daily-review-detail")

    async def on_mount(self) -> None:
        await init_db(self.engine)
        await self._load_db_data()

        all_contests = [
            {
                "title": week["week"],
                "week_index": index,
            }
            for index, week in enumerate(self.data["weeks"])
        ]
        scraper = VJudgeScraper()
        active_contest = scraper.find_active_contest(all_contests)
        if isinstance(active_contest, dict):
            self.selected_week_index = int(active_contest.get("week_index", 0))
            active_title = str(active_contest.get("title", "")).strip()
            match = re.search(
                r"\[.*?(26s|26spring).*?\]\s*L(\d+):\s*(.*)",
                active_title,
                flags=re.IGNORECASE,
            )
            if match is not None:
                lecture_num = int(match.group(2))
                lecture_topic = match.group(3).strip()
                self.title = f"Current Practice: [L{lecture_num}] {lecture_topic}"
            elif active_title:
                self.title = f"Current Practice: {active_title}"

        await self._populate_weeks()
        await self._populate_problems()
        await self._populate_daily_review()
        self._update_markdown()

    async def _load_db_data(self) -> None:
        async with self.session_maker() as session:
            result = await session.execute(select(Problem.contest_id).distinct().order_by(Problem.contest_id))
            contests = [contest_id for contest_id in result.scalars().all() if contest_id]

        self.data = {
            "weeks": [
                {
                    "week": contest_id,
                    "problems": [],
                }
                for contest_id in contests
            ]
        }

        self.selected_week_index = 0
        self.selected_problem_index = 0

    async def _load_problems_for_contest(self, contest_id: str) -> list[ProblemData]:
        problem_rows: list[ProblemData] = []
        async with self.session_maker() as session:
            problems_result = await session.execute(
                select(Problem).where(Problem.contest_id == contest_id).order_by(Problem.id)
            )
            problems = list(problems_result.scalars().all())

            for problem in problems:
                latest_attempt_result = await session.execute(
                    select(Attempt)
                    .where(Attempt.problem_id == problem.id)
                    .order_by(Attempt.timestamp.desc())
                    .limit(1)
                )
                latest_attempt = latest_attempt_result.scalar_one_or_none()
                if latest_attempt is None:
                    status = "unsolved"
                elif latest_attempt.status == AttemptStatus.PASS:
                    status = "solved"
                else:
                    status = "attempted"

                problem_rows.append(
                    {
                        "id": problem.id,
                        "title": problem.title,
                        "contest_id": problem.contest_id,
                        "status": status,
                        "description": problem.html_content,
                    }
                )

        return problem_rows

    async def _populate_weeks(self) -> None:
        week_list = self.query_one("#weekly-list", VerticalScroll)
        await week_list.remove_children()

        for index, week in enumerate(self.data["weeks"]):
            week_button = Button(
                week["week"],
                id=f"contest-{index}",
                classes="list-item",
                variant="default",
            )
            if index == self.selected_week_index:
                week_button.add_class("is-selected")
            await week_list.mount(week_button)

    async def _populate_problems(self) -> None:
        problem_list = self.query_one("#problem-list", VerticalScroll)
        await problem_list.remove_children()

        weeks = self.data["weeks"]
        if not weeks:
            self.query_one("#detail-markdown", Markdown).update("No data found.")
            return

        current_week = weeks[self.selected_week_index]
        problems = await self._load_problems_for_contest(current_week["week"])
        self.data["weeks"][self.selected_week_index]["problems"] = problems
        if not problems:
            self.query_one("#detail-markdown", Markdown).update("No problems found for this week.")
            return

        self.selected_problem_index = min(self.selected_problem_index, len(problems) - 1)

        for index, problem in enumerate(problems):
            icon = self._status_icon(problem["status"])
            label = f"{icon} {problem['title']}"
            problem_button = Button(
                label,
                id=f"problem-{index}",
                classes="list-item",
                variant="default",
            )
            if index == self.selected_problem_index:
                problem_button.add_class("is-selected")
            await problem_list.mount(problem_button)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id is None:
            return

        if button_id.startswith("contest-"):
            self.selected_week_index = int(button_id.split("-", maxsplit=1)[1])
            self.selected_problem_index = 0
            await self._populate_weeks()
            await self._populate_problems()
            self._update_markdown()
            return

        if button_id.startswith("problem-"):
            self.selected_problem_index = int(button_id.split("-", maxsplit=1)[1])
            await self._populate_problems()
            self._update_markdown()
            return

        if button_id.startswith("review-"):
            self.selected_daily_problem_id = int(button_id.split("-", maxsplit=1)[1])
            await self._update_daily_review_markdown()

    async def action_open_problem(self) -> None:
        problem = self._selected_problem()
        if problem is None:
            return

        workspace_dir = await asyncio.to_thread(
            self.workflow.setup_workspace,
            problem["contest_id"],
            str(problem["id"]),
        )
        solution_path = workspace_dir / "solution.cpp"
        self.active_problem_workspaces[problem["id"]] = ProblemWorkspaceState(
            solution_path=solution_path,
            started_at=datetime.utcnow(),
        )
        await asyncio.to_thread(self.workflow.open_editor, solution_path)

    async def action_run_tests(self) -> None:
        problem = self._selected_problem()
        if problem is None:
            return

        workspace_state = self.active_problem_workspaces.get(problem["id"])
        if workspace_state is None:
            workspace_dir = await asyncio.to_thread(
                self.workflow.setup_workspace,
                problem["contest_id"],
                str(problem["id"]),
            )
            workspace_state = ProblemWorkspaceState(
                solution_path=workspace_dir / "solution.cpp",
                started_at=datetime.utcnow(),
            )
            self.active_problem_workspaces[problem["id"]] = workspace_state

        passed, output = await asyncio.to_thread(self.workflow.run_tests, workspace_state.solution_path)

        result_body = output if output else "All tests passed."
        result_title = "✅ Tests Passed" if passed else "❌ Tests Failed"
        await self.push_screen_wait(TestResultModal(result_title, result_body))

        await self._record_attempt(problem_id=problem["id"], passed=passed, started_at=workspace_state.started_at)
        await self._populate_problems()
        self._update_markdown()

        if not passed:
            return

        difficulty = await self.push_screen_wait(DifficultyModal())
        if difficulty is None:
            await self._populate_daily_review()
            return

        await self._update_fsrs(problem_id=problem["id"], difficulty=difficulty)
        await self._populate_daily_review()

    def _selected_problem(self) -> ProblemData | None:
        weeks = self.data["weeks"]
        if not weeks:
            return None

        problems = weeks[self.selected_week_index]["problems"]
        if not problems:
            return None

        if self.selected_problem_index >= len(problems):
            return None

        return problems[self.selected_problem_index]

    async def _record_attempt(self, problem_id: int, passed: bool, started_at: datetime) -> None:
        duration_seconds = max(1, int((datetime.utcnow() - started_at).total_seconds()))
        status = AttemptStatus.PASS if passed else AttemptStatus.FAIL

        async with self.session_maker() as session:
            session.add(
                Attempt(
                    problem_id=problem_id,
                    status=status,
                    duration=duration_seconds,
                )
            )
            await session.commit()

    async def _update_fsrs(self, problem_id: int, difficulty: int) -> None:
        now = datetime.utcnow()
        async with self.session_maker() as session:
            fsrs_result = await session.execute(select(FSRSState).where(FSRSState.problem_id == problem_id))
            state = fsrs_result.scalar_one_or_none()

            if state is None:
                previous = FSRSReviewState(stability=0.5, difficulty=5.0, last_reviewed=now)
                elapsed_days = 0.0
            else:
                previous = FSRSReviewState(
                    stability=state.stability,
                    difficulty=state.difficulty,
                    last_reviewed=state.last_reviewed,
                )
                elapsed_days = max(0.0, (now - state.last_reviewed).total_seconds() / 86_400)

            update = calculate_next_review(previous, difficulty, elapsed_days)

            if state is None:
                session.add(
                    FSRSState(
                        problem_id=problem_id,
                        stability=update.stability,
                        difficulty=update.difficulty,
                        last_reviewed=update.last_reviewed,
                        next_review_date=update.next_review_date,
                    )
                )
            else:
                state.stability = update.stability
                state.difficulty = update.difficulty
                state.last_reviewed = update.last_reviewed
                state.next_review_date = update.next_review_date

            await session.commit()

    async def _populate_daily_review(self) -> None:
        review_list = self.query_one("#daily-review-list", VerticalScroll)
        await review_list.remove_children()

        end_of_today = datetime.combine(date.today(), time.max)
        async with self.session_maker() as session:
            review_result = await session.execute(
                select(Problem, FSRSState)
                .join(FSRSState, FSRSState.problem_id == Problem.id)
                .where(FSRSState.next_review_date <= end_of_today)
                .order_by(FSRSState.next_review_date.asc())
            )
            rows = review_result.all()

        if not rows:
            self.query_one("#daily-review-detail", Markdown).update("No problems due today.")
            return

        for problem, state in rows:
            button = Button(
                f"{problem.contest_id} • {problem.title} (due {state.next_review_date.date().isoformat()})",
                id=f"review-{problem.id}",
                classes="list-item",
                variant="default",
            )
            if self.selected_daily_problem_id == problem.id:
                button.add_class("is-selected")
            await review_list.mount(button)

        if self.selected_daily_problem_id is None:
            self.selected_daily_problem_id = rows[0][0].id

        await self._update_daily_review_markdown()

    async def _update_daily_review_markdown(self) -> None:
        detail = self.query_one("#daily-review-detail", Markdown)
        if self.selected_daily_problem_id is None:
            detail.update("No review item selected.")
            return

        async with self.session_maker() as session:
            row_result = await session.execute(
                select(Problem, FSRSState)
                .join(FSRSState, FSRSState.problem_id == Problem.id)
                .where(Problem.id == self.selected_daily_problem_id)
            )
            row = row_result.first()

        if row is None:
            detail.update("Selected review problem was not found.")
            return

        problem, fsrs_state = row
        detail.update(
            "\n".join(
                [
                    f"## {problem.title}",
                    f"Contest: **{problem.contest_id}**",
                    f"Stability: **{fsrs_state.stability:.2f}**",
                    f"Difficulty: **{fsrs_state.difficulty:.2f}**",
                    f"Next review: **{fsrs_state.next_review_date.isoformat(sep=' ', timespec='seconds')}**",
                ]
            )
        )

    def _update_markdown(self) -> None:
        markdown = self.query_one("#detail-markdown", Markdown)
        weeks = self.data["weeks"]
        if not weeks:
            markdown.update("No training data available.")
            return

        problems = weeks[self.selected_week_index]["problems"]
        if not problems:
            markdown.update("No problems available for this contest week.")
            return

        problem = problems[self.selected_problem_index]
        details = (
            f"## {problem['title']}\\n"
            f"Contest: **{weeks[self.selected_week_index]['week']}**\\n"
            f"Status: {self._status_icon(problem['status'])} **{problem['status'].title()}**\\n\\n"
            f"{problem['description']}"
        )
        markdown.update(details)

    @staticmethod
    def _status_icon(status: str) -> str:
        icon_map = {
            "solved": "✅",
            "attempted": "🟡",
            "unsolved": "⚪",
        }
        return icon_map.get(status.lower(), "❔")


def main() -> None:
    app = ICPCTrainerApp()
    app.run()


if __name__ == "__main__":
    main()
