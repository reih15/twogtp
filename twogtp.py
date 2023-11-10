from __future__ import annotations

import argparse
import datetime
import os
import re
import string
import subprocess
import sys
import threading
import zlib
from signal import SIGINT
from typing import Iterable, Optional

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
args = vars(parser.parse_args())

print(f"[twogtp] args: {args}\n")

engine_1_cmd = re.split(r"\s+", args["black"].rstrip())
engine_1_name = args["black_name"]
engine_2_cmd = re.split(r"\s+", args["white"].rstrip())
engine_2_name = args["white_name"]

alternate = args["alternate"]
size = args["size"]
komi = args["komi"]
games = args["games"]
maxmoves = args["maxmoves"]

sgfs_dir = args["sgfs_dir"]


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
                self._proc.stdin.write(f"{command}\n".encode("utf-8"))
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

            line = self._proc.stdout.readline().decode("utf-8").replace(os.linesep, "\n")
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


class SGF:
    def __init__(self) -> None:
        self.gm = 1
        self.ff = 4
        self.ca = "utf-8"
        self.sz = 19

        self.dt: Optional[str] = None
        self.pb: Optional[str] = None
        self.pw: Optional[str] = None

        self.km = 6.5
        self.re: Optional[str] = None

        self.moves: list[tuple[str, str]] = []

    def add_move(self, color: str, move: str) -> None:
        self.moves.append((color, move))

    def add_move_from_genmove_return(self, color: str, genmove_return: str) -> None:
        if genmove_return == "pass":
            self.add_move(color, "")
            return

        genmove_column_labels = list(string.ascii_lowercase.replace("i", "")[: self.sz])
        move_labels = list(string.ascii_lowercase[: self.sz])
        c = genmove_return[0]
        r = genmove_return[1:]
        move = f"{move_labels[genmove_column_labels.index(c)]}{move_labels[self.sz - int(r)]}"
        self.add_move(color, move)

    def gen_file_name(self, data_for_hash: str) -> str:
        now = now_jst()
        h = zlib.crc32(data_for_hash.encode())
        if self.pb and self.pw:
            return f"{now:%Y%m%d-%H%M%S}_{self.pb}-{self.pw}_{h:08x}.sgf"
        else:
            return f"{now:%Y%m%d-%H%M%S}_{h:08x}.sgf"

    def join_all_data(self) -> str:
        root_gameinfo_properties = [";", f"GM[{self.gm}]", f"FF[{self.ff}]", f"CA[{self.ca}]", f"SZ[{self.sz}]"]
        if self.dt is not None:
            root_gameinfo_properties.append(f"DT[{self.dt}]")
        if self.pb is not None:
            root_gameinfo_properties.append(f"PB[{self.pb}]")
        if self.pw is not None:
            root_gameinfo_properties.append(f"PW[{self.pw}]")
        root_gameinfo_properties.append(f"KM[{self.km}]")
        if self.re is not None:
            root_gameinfo_properties.append(f"RE[{self.re}]")

        root_gameinfo_joined = "".join(root_gameinfo_properties)

        end_pass_count = 0
        for _, move in reversed(self.moves):
            if move == "":
                end_pass_count += 1
            else:
                break
        end_passes_removed_moves = self.moves
        if end_pass_count >= 2:
            end_passes_removed_moves = self.moves[:-end_pass_count]

        moves_joined = "".join([f";{c.upper()}[{move}]" for c, move in end_passes_removed_moves])

        return f"({root_gameinfo_joined}{moves_joined})\n"

    def dump_to_file(self, file_path: str, joined_data: Optional[str] = None) -> None:
        if joined_data is None:
            joined_data = self.join_all_data()

        with open(file_path, "w", encoding=self.ca) as f:
            f.write(joined_data)

    def dump_in_dir(self, dir_path: str) -> None:
        abs_dir_path = os.path.abspath(dir_path)
        joined_data = self.join_all_data()
        self.dump_to_file(f"{abs_dir_path}{os.path.sep}{self.gen_file_name(joined_data)}", joined_data)


def send_command_to_engines(command: str, engines: Iterable[GTPEngine]) -> None:
    for engine in engines:
        engine.communicate(command)


def setup_engines(engines: Iterable[GTPEngine]) -> None:
    setup_commands = ["clear_board", f"boardsize {size}", f"komi {komi}"]
    for command in setup_commands:
        send_command_to_engines(command, engines)


def synchronize_engine(engine: GTPEngine, move_history: list[tuple[str, str]]) -> None:
    for color, move in move_history:
        if move != "pass":
            engine.communicate(f"play {color} {move}")


def format_response_line(response_lines: list[str]) -> str:
    return re.sub(r"^[\s=]*", "", response_lines[0].rstrip())


def quit_engines(engines: Iterable[GTPEngine]) -> None:
    send_command_to_engines("quit", engines)
    for engine in engines:
        engine.proc.wait()


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

        sgf = SGF()
        sgf.sz = size
        sgf.km = komi
        sgf.pb, sgf.pw = black.name, white.name

        move_history = []
        last_move_is_pass = False
        for move_num in range(1, maxmoves + 1):
            if move_num % 2 == 1:
                color = "b"
                now_turn = black
                the_other = white
            else:
                color = "w"
                now_turn = white
                the_other = black

            move = format_response_line(now_turn.communicate(f"genmove {color}")).lower()
            if move == "resign":
                if color == "b":
                    sgf.re = "W+R"
                else:
                    sgf.re = "B+R"
                break
            else:
                move_history.append((color, move))
                if move == "pass":
                    if last_move_is_pass:
                        setup_engines([referee])
                        synchronize_engine(referee, move_history)
                        result = format_response_line(referee.communicate("final_score"))
                        sgf.re = result.upper()
                        break
                    else:
                        last_move_is_pass = True
                else:
                    the_other.communicate(f"play {color} {move}")
                    last_move_is_pass = False

        if sgf.re is None:
            sgf.re = "Void"

        now = now_jst()
        sgf.dt = f"{now:%Y-%m-%d}"

        for color, move in move_history:
            sgf.add_move_from_genmove_return(color, move)

        os.makedirs(sgfs_dir, exist_ok=True)
        sgf.dump_in_dir(sgfs_dir)

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
