class ParseError(Exception):
    def __init__(self, s, i, msg):
        line, col = lineCol(s, i)
        self.msg = f"Parse error on line {line} col {col}:"
        if "\n" in msg:
            self.msg += "\n" + msg
        elif len(self.msg) + len(msg) + 1 > 78:
            self.msg += "\n  " + msg
        else:
            self.msg += " " + msg
        super().__init__(self.msg)


def lineCol(s, index):
    """Determines the line and column from an index."""
    line = 0
    col = 0
    for i in range(index):
        if s[i] == "\n":
            line += 1
            col = 0
            continue
        col += 1
    return line, col
