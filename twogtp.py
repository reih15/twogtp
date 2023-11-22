from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import threading
import time
import zlib
from signal import SIGINT
from typing import Iterable, Optional
from urllib.parse import quote

from pysgf import Move, SGFNode

ENCODING = "utf-8"
SGFVIEWER_URL_PREFIX = "https://reih15.github.io/SGFViewer/view.html?sgf="


class GTPEngine:
    def __init__(self, cmd: list[str], name: str) -> None:
        print(f"[twogtp] engine command: \"{' '.join(cmd)}\"", file=sys.stderr)
        print(f'[twogtp] engine name: "{name}"\n', file=sys.stderr)
        self._name = name
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)

    @property
    def proc(self) -> subprocess.Popen[bytes]:
        return self._proc

    @property
    def name(self) -> str:
        return self._name

    def write_command(self, command: str) -> None:
        def write_command_target() -> None:
            if self._proc.stdin is not None:
                self._proc.stdin.write(f"{command}\n".encode(ENCODING))
                self._proc.stdin.flush()
            else:
                print("[twogtp] proc.stdin is None")

        t = threading.Thread(target=write_command_target)
        t.start()
        t.join()

    def read_response_lines(self) -> list[str]:
        lines = []
        response_started = False
        while True:
            if self._proc.stdout is None:
                print("[twogtp] proc.stdout is None")
                return []

            line = self._proc.stdout.readline().decode(ENCODING).replace(os.linesep, "\n")
            if line.startswith("=") or line.startswith("?"):
                response_started = True
            if response_started:
                lines.append(line)
            if line == "\n":
                break
        return lines

    def communicate(self, command: str) -> list[str]:
        print(f"[twogtp] {self._name} <- {command}")
        self.write_command(command)
        response_lines = self.read_response_lines()
        print(f"[twogtp] {self._name} ->")
        for line in response_lines:
            print(line, end="")
        return response_lines


def now_jst() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))


class GameData:
    def __init__(self) -> None:
        self.date: Optional[str] = None
        self.player_black: Optional[str] = None
        self.player_white: Optional[str] = None

        self.size = 19
        self.komi = 6.5
        self.result: Optional[str] = None

        self.moves: list[Move] = []

    def add_move(self, move: Move) -> None:
        self.moves.append(move)

    def sgf(self) -> str:
        root = SGFNode(properties={"GM": 1, "FF": 4, "CA": ENCODING, "SZ": self.size, "KM": self.komi})
        if self.date is not None:
            root.set_property("DT", self.date)
        if self.player_black is not None:
            root.set_property("PB", self.player_black)
        if self.player_white is not None:
            root.set_property("PW", self.player_white)
        if self.result is not None:
            root.set_property("RE", self.result)

        end_pass_count = 0
        for move in reversed(self.moves):
            if move.gtp() == "pass":
                end_pass_count += 1
            else:
                break
        end_passes_removed_moves = self.moves
        if end_pass_count >= 2:
            end_passes_removed_moves = self.moves[:-end_pass_count]

        prev_node = root
        for move in end_passes_removed_moves:
            node = SGFNode(parent=prev_node, move=move)
            prev_node = node

        return root.sgf()

    def _gen_file_name(self, data_for_hash: str) -> str:
        now = now_jst()
        h = zlib.crc32(data_for_hash.encode())
        if self.player_black and self.player_white:
            return f"{now:%Y%m%d-%H%M%S}_{self.player_black}-{self.player_white}_{h:08x}.sgf"
        else:
            return f"{now:%Y%m%d-%H%M%S}_{h:08x}.sgf"

    def dump_sgf_in_dir(self, dir_path: str) -> None:
        abs_dir_path = os.path.abspath(dir_path)
        sgf = self.sgf()
        file_path = f"{abs_dir_path}{os.path.sep}{self._gen_file_name(sgf)}"
        with open(file_path, "w", encoding=ENCODING) as f:
            f.write(sgf)


def send_command_to_engines(command: str, engines: Iterable[GTPEngine]) -> None:
    for engine in engines:
        engine.communicate(command)


def setup_engines(engines: Iterable[GTPEngine]) -> None:
    setup_commands = ["clear_board", f"boardsize {size}", f"komi {komi}"]
    for command in setup_commands:
        send_command_to_engines(command, engines)


def synchronize_engine(engine: GTPEngine, moves: list[Move]) -> None:
    for move in moves:
        gtp_coords = move.gtp()
        if gtp_coords != "pass":
            engine.communicate(f"play {move.player} {gtp_coords}")


def format_response_line(response_lines: list[str]) -> str:
    return re.sub(r"^[\s=]*", "", response_lines[0].rstrip())


def quit_engines(engines: Iterable[GTPEngine]) -> None:
    send_command_to_engines("quit", engines)
    for engine in engines:
        engine.proc.wait()


parser = argparse.ArgumentParser()
parser.add_argument("--black", required=True)
parser.add_argument("--white", required=True)
parser.add_argument("--referee")
parser.add_argument("--black_name", default="b")
parser.add_argument("--white_name", default="w")
parser.add_argument("--alternate", action="store_true")
parser.add_argument("--size", type=int, default=19)
parser.add_argument("--komi", type=float, default=6.5)
parser.add_argument("--games", type=int, default=1)
parser.add_argument("--maxmoves", type=int, default=1000)
parser.add_argument("--sgfs_dir", default="sgfs")
parser.add_argument("--sleep_sec", type=float, default=0)
args = vars(parser.parse_args())

print(f"[twogtp] args: {args}\n")

alternate = args["alternate"]
size = args["size"]
komi = args["komi"]
games = args["games"]
maxmoves = args["maxmoves"]

sgfs_dir = args["sgfs_dir"]

sleep_sec = args["sleep_sec"]

engine_1_cmd = re.split(r"\s+", args["black"].rstrip())
engine_1_name = args["black_name"]
engine_2_cmd = re.split(r"\s+", args["white"].rstrip())
engine_2_name = args["white_name"]
engine_1 = GTPEngine(engine_1_cmd, engine_1_name)
engine_2 = GTPEngine(engine_2_cmd, engine_2_name)
all_engines = [engine_1, engine_2]
referee = engine_1
if args["referee"] is not None:
    referee_cmd = re.split(r"\s+", args["referee"].rstrip())
    referee = GTPEngine(referee_cmd, "referee")
    all_engines.append(referee)


def game_loop() -> None:
    black = engine_1
    white = engine_2
    for game_num in range(1, games + 1):
        print(f"[twogtp] Game {game_num}:")

        setup_engines([black, white])

        game_data = GameData()
        game_data.size = size
        game_data.komi = komi
        game_data.player_black = black.name
        game_data.player_white = white.name

        last_move_is_pass = False
        for move_num in range(1, maxmoves + 1):
            if move_num % 2 == 1:
                color = "B"
                now_turn = black
                the_other = white
            else:
                color = "W"
                now_turn = white
                the_other = black

            gtp_coords = format_response_line(now_turn.communicate(f"genmove {color}")).lower()
            if gtp_coords == "resign":
                if color == "B":
                    game_data.result = "W+R"
                else:
                    game_data.result = "B+R"
                break
            else:
                game_data.add_move(Move.from_gtp(gtp_coords.upper(), color))
                if gtp_coords == "pass":
                    if last_move_is_pass:
                        setup_engines([referee])
                        synchronize_engine(referee, game_data.moves)
                        result = format_response_line(referee.communicate("final_score"))
                        game_data.result = result.upper()
                        break
                    else:
                        last_move_is_pass = True
                else:
                    the_other.communicate(f"play {color} {gtp_coords}")
                    last_move_is_pass = False

            print(f"[twogtp] {SGFVIEWER_URL_PREFIX}{quote(game_data.sgf())}")

            if sleep_sec > 0:
                print(f"[twogtp] sleep {sleep_sec}")
                time.sleep(sleep_sec)

        if game_data.result is None:
            game_data.result = "Void"

        now = now_jst()
        game_data.date = f"{now:%Y-%m-%d}"

        print(f"[twogtp] {SGFVIEWER_URL_PREFIX}{quote(game_data.sgf())}")

        os.makedirs(sgfs_dir, exist_ok=True)
        game_data.dump_sgf_in_dir(sgfs_dir)

        if alternate:
            tmp = black
            black = white
            white = tmp


try:
    game_loop()
    quit_engines(all_engines)
except KeyboardInterrupt:
    print("[twogtp] Interrupted")

    for engine in all_engines:
        engine.proc.send_signal(SIGINT)

    wait_sec = 5
    for engine in all_engines:
        try:
            engine.proc.wait(wait_sec)
        except subprocess.TimeoutExpired:
            engine.proc.kill()
    sys.exit()
except Exception as e:
    print("[twogtp] Unexpected Exception")
    print(f"[twogtp] {e}")
    for engine in all_engines:
        engine.proc.kill()
    sys.exit(1)
