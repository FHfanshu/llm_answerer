"""
终端 Dashboard - 显示服务状态和统计
"""
import time
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box


class Dashboard:
    def __init__(self):
        self.console = Console()
        self.start_time = time.time()
        self.requests: list[dict] = []  # 最近请求
        self.max_log = 10

        # 统计
        self.total = 0
        self.bank_hits = 0
        self.db_hits = 0
        self.errors = 0
        self.total_tokens = 0
        self.total_elapsed = 0.0

    def record(self, title: str, answer: str, elapsed: float, source: str, tokens: int = 0):
        """记录一次请求"""
        self.total += 1
        if source == "bank":
            self.bank_hits += 1
        elif source == "db":
            self.db_hits += 1
        elif source == "error":
            self.errors += 1

        self.total_tokens += tokens
        self.total_elapsed += elapsed

        self.requests.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "title": title[:30],
            "answer": answer[:10],
            "elapsed": f"{elapsed:.1f}s",
            "source": source
        })
        if len(self.requests) > self.max_log:
            self.requests.pop(0)

    @property
    def hit_rate(self) -> float:
        return (self.bank_hits + self.db_hits) / self.total * 100 if self.total > 0 else 0

    @property
    def avg_elapsed(self) -> float:
        return self.total_elapsed / self.total if self.total > 0 else 0

    def build_header(self, model: str, port: int, bank_size: int) -> Panel:
        """构建头部信息"""
        uptime = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        text = Text()
        text.append("  LLM 答题服务\n\n", style="bold cyan")
        text.append("  模型: ", style="dim")
        text.append(f"{model}\n", style="bold green")
        text.append("  端口: ", style="dim")
        text.append(f"{port}\n", style="bold green")
        text.append("  题库: ", style="dim")
        text.append(f"{bank_size} 条\n", style="bold green")
        text.append("  运行: ", style="dim")
        text.append(f"{hours:02d}:{minutes:02d}:{seconds:02d}", style="bold green")

        return Panel(text, title="[bold]服务状态[/bold]", border_style="cyan", box=box.ROUNDED)

    def build_stats(self) -> Panel:
        """构建统计面板"""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("指标", style="dim")
        table.add_column("值", style="bold")

        table.add_row("总请求", str(self.total))
        table.add_row("题库命中", f"{self.bank_hits} [green]({self.bank_hits/self.total*100:.0f}%)[/green]" if self.total > 0 else "0")
        table.add_row("数据库命中", f"{self.db_hits} [yellow]({self.db_hits/self.total*100:.0f}%)[/yellow]" if self.total > 0 else "0")
        table.add_row("总命中率", f"[bold cyan]{self.hit_rate:.1f}%[/bold cyan]")
        table.add_row("错误", f"[red]{self.errors}[/red]" if self.errors > 0 else "0")
        table.add_row("Token 消耗", f"~{self.total_tokens:,}")
        table.add_row("平均耗时", f"{self.avg_elapsed:.1f}s")

        return Panel(table, title="[bold]统计[/bold]", border_style="green", box=box.ROUNDED)

    def build_log(self) -> Panel:
        """构建请求日志"""
        if not self.requests:
            text = Text("  等待请求...", style="dim italic")
        else:
            table = Table(show_header=True, box=None, padding=(0, 1))
            table.add_column("时间", style="dim", width=8)
            table.add_column("来源", width=6)
            table.add_column("题目", width=25)
            table.add_column("答案", width=8)
            table.add_column("耗时", width=6)

            for req in reversed(self.requests):
                source_style = {
                    "bank": "[green]题库[/green]",
                    "db": "[yellow]缓存[/yellow]",
                    "llm": "[cyan]LLM[/cyan]",
                    "error": "[red]错误[/red]"
                }.get(req["source"], req["source"])

                table.add_row(
                    req["time"],
                    source_style,
                    req["title"],
                    req["answer"],
                    req["elapsed"]
                )

            text = table

        return Panel(text, title="[bold]请求日志[/bold]", border_style="yellow", box=box.ROUNDED)

    def render(self, model: str, port: int, bank_size: int) -> Layout:
        """渲染完整 dashboard"""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=10),
            Layout(name="body")
        )
        layout["body"].split_row(
            Layout(name="stats", ratio=1),
            Layout(name="log", ratio=2)
        )

        layout["header"].update(self.build_header(model, port, bank_size))
        layout["stats"].update(self.build_stats())
        layout["log"].update(self.build_log())

        return layout


# 全局实例
dashboard = Dashboard()
