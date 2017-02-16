#!/usr/bin/env python3

"""Incremental readline compatible with micropyton/lib/readline.c"""

from getch import getch
import logging
import signal
import sys

CTRL_A  = b'\x01'
CTRL_C  = b'\x03'
CTRL_D  = b'\x04'
CTRL_E  = b'\x05'
CTRL_U  = b'\x15'
BS      = b'\x08'
TAB     = b'\x09'
CR      = b'\x0d'
LF      = b'\x0a'
ESC     = b'\x1b'
DEL     = b'\x7f'

# The following escape sequence is used to query the size of the window:
#
# ESC 7         - Save cursor position
# ESC [r        - Enable scrolling for entire display
# ESC [row;colH - Move to cursor position
# ESC [6n       - Device Status Report - send ESC [row;colR
# ESC 8         - Restore cursor position

REPORT_WINDOW_SIZE_1 = b'\x1b7\x1b[r\x1b[999;999H'
REPORT_WINDOW_SIZE_2 = b'\x1b8'
REPORT_CURSOR_LOCATION = b'\x1b[6n'

# When running the test suite, we're only checking the line buffer, so we
# disable output
TESTING = False

def printable(ch):
    """Returns a printable representation of a character."""
    val = ord(ch)
    if val < ord(' ') or val > ord('~'):
        return '.'
    return chr(val)


class CmdBase(object):

    def __init__(self, log=None):
        self.log = log or logging.getLogger(__name__)


class CmdWrite(CmdBase):

    def __init__(self, string, log=None):
        CmdBase.__init__(self, log)
        self.string = string

    def is_input(self):
        return False

    def process(self):
        if TESTING:
            return
        self.log.debug('CmdWrite(' + repr(self.string) + ')')
        if isinstance(self.string, str):
            sys.stdout.write(self.string)
            sys.stdout.flush()
        else:
            sys.stdout.buffer.write(self.string)
            sys.stdout.buffer.flush()


class CmdInput(CmdBase):

    def __init__(self, func, log=None):
        CmdBase.__init__(self, log)
        self._func = func

    def is_input(self):
        return True

    def process(self):
        # For some reason, the ESC [ 999;999 R sequence doesn't cause
        # select to trigger. So we do a read here.
        # This is what ncurses does as well.
        data = ''
        while True:
            char = sys.stdin.read(1)
            self.log.debug("CmdInput: got char '%c' 0x%02x" % (printable(char), ord(char)))
            if char == 'R':
                break
            data += char
        if data[0] != chr(ord(ESC)) or data[1] != '[':
            self.log.error('Invalid cursor position received')
            self.log.error('data[0] = ' + repr(data[0]))
            self.log.error('data[1] = ' + repr(data[1]))
            return
        num_str = data[2:].split(';')
        try:
            rows = int(num_str[0])
            cols = int(num_str[1])
        except:
            self.log.error("Unknown ESC [ '%s' R" % data[2:])
            self.log.error('num_str = ' + repr(num_str))
            return
        self.log.debug("CmdInput: %s rows: %d cols: %d" %
                       (self._func.__name__, rows, cols))
        self._func(rows, cols)


class CmdWriteQueue(object):

    def __init__(self, log=None):
        self.log = log or logging.getLogger(__name__)
        self.queue = []

    def write(self, string):
        self.queue.append(CmdWrite(string, log=self.log))

    def queue_input(self, func):
        self.queue.append(CmdInput(func, log=self.log))

    def wait_for_input(self, func):
        self.queue.append(CmdInput(func, log=self.log))

    def process(self):
        while len(self.queue) > 0:
            cmd = self.queue.pop(0)
            cmd.process()

    def process_input(self, *args, **kwargs):
        assert len(self.queue) > 0
        assert self.queue[0].is_input()
        cmd = self.queue.pop(0)
        cmd.process()
        self.process()


class mp_readline(object):

    # We arrange the state machine such that the current state is a dictionary
    # which contains the action routines to execute when a particular character
    # is received. The key is the character, and the value is the routine
    # to execute.
    #
    # The None key is used to process any characters which don't otherwise
    # appear in the state dictionary, and it will be passed the received
    # character as an argument.

    def __init__(self, prompt=None, log=None):
        self.ESEQ_NONE = {
            CTRL_A : self.home,
            CTRL_C : self.cancel,
            CTRL_D : self.eof,
            CTRL_E : self.end,
            CTRL_U : self.clear_before_cursor,
            BS     : self.backspace,
            CR     : self.line_complete,
            ESC    : self.esc,
            DEL    : self.backspace,
            None   : self.typed_char,
        }
        self.ESEQ_ESC = {
            b'['    : self.esc_bracket,
            b'O'    : self.esc_O,
            None    : self.esc_typed_char,
        }
        self.ESEQ_ESC_BRACKET = {
            b'A'    : self.up_arrow,
            b'B'    : self.down_arrow,
            b'C'    : self.right_arrow,
            b'D'    : self.left_arrow,
            b'H'    : self.home,
            b'F'    : self.end,
            None    : self.esc_bracket_typed_char,
        }
        self.ESEQ_ESC_BRACKET_DIGIT = {
            b'R'    : self.esc_bracket_digit_R,
            b'~'    : self.esc_bracket_digit_tilde,
            None    : self.esc_bracket_digit_typed_char,
        }
        self.ESEQ_ESC_O = {
            b'H'    : self.home,
            b'F'    : self.end,
            None    : self.esc_O_typed_char,
        }
        self.state = self.ESEQ_NONE
        self.log = log or logging.getLogger(__name__)
        self.rows = 0
        self.columns = 0
        self.prompt = ''
        self.overwrite = False
        self.write_queue = CmdWriteQueue(log)
        self.get_window_size()
        # Note to self: Apparently Windows will raise a value error for the following
        signal.signal(signal.SIGWINCH, self.handle_sigwinch)
        self.reset(prompt)

    def reset(self, prompt=None):
        self.line = ''
        self.esc_seq = ''
        self.caret = 0      # position within line that data entry will occur
        self.cursor_col = 0 # place that cursor is on the screen
        self.line_start = 0 # index of first character to draw
        self.prompt_width = 0
        self.inval_start = -1
        self.inval_end = -1
        self.input_width = 80
        self.resized = False
        if prompt is None:
            prompt = self.prompt
        self.set_prompt(prompt)
        self.write_queue.process()

    def handle_sigwinch(self, signum, frame):
        """Called when the terminal console changes sizes."""
        # It's not safe to do very much during a signal handler, so we just
        # set a flag that indicates that a signal was received and that's it.
        self.log.debug("SIGWINCH received")
        self.resized = True

    def get_cursor_location(self, func):
        if TESTING:
            return
        self.write(REPORT_CURSOR_LOCATION)
        self.write_queue.queue_input(func)

    def get_window_size(self):
        if  TESTING:
            return
        self.write(REPORT_WINDOW_SIZE_1)
        self.get_cursor_location(self.store_window_size)
        self.write(REPORT_WINDOW_SIZE_2)

    def store_window_size(self, rows, cols):
        self.rows = rows
        self.columns = cols
        if self.resized:
            self.redraw()

    def set_prompt(self, prompt):
        self.prompt = prompt
        if prompt:
            self.write(prompt)
            self.get_cursor_location(self.store_prompt_width)

    def store_prompt_width(self, rows, cols):
        self.prompt_width = cols

    def backspace(self):
        if self.caret > 0:
            old_line_len = len(self.line)
            self.line = self.line[:self.caret - 1] + self.line[self.caret:]
            self.caret -= 1
            self.invalidate(self.caret, old_line_len)

    def cancel(self):
        """Cancels the current line."""
        self.line = ''
        return self.line

    def clear_before_cursor(self):
        old_line_len = len(line)
        self.line = self.line[self.caret:]
        self.caret = 0
        self.invalidate(0, old_line_len)

    def delete(self):
        """Delete the character to the right of the cursor."""
        if self.caret < len(self.line):
            old_line_len = len(self.line)
            self.line = self.line[:self.caret] + self.line[self.caret + 1:]
            self.invalidate(self.caret, old_line_len)

    def down_arrow(self):
        self.state = self.ESEQ_NONE

    def end(self):
        """Moves the cursor to the end of the line."""
        self.caret = len(self.line)
        self.state = self.ESEQ_NONE

    def eof(self):
        if len(self.line) == 0:
            self.write('\r\n')
            self.write_queue.process()
            raise EOFError
        # Control-D acts like delete when the line is not empty
        return self.delete()

    def esc(self):
        """Starts an ESC sequence."""
        self.state = self.ESEQ_ESC
        self.esc_seq = ''

    def esc_bracket(self):
        """Starts an ESC [ sequence."""
        self.state = self.ESEQ_ESC_BRACKET

    def esc_bracket_digit_R(self):
        """Handle ESC [ 999 ; 999 R."""
        self.state = self.ESEQ_NONE
        num_str = self.esc_seq.split(';')
        try:
            rows = int(num_str[0])
            columns = int(num_str[1])
        except:
            self.log.error("Unknown ESC [ '%s' R" % self.esc_seq)
            return
        self.write_queue.process_input(rows, columns)
        self.esc_seq = ''

    def esc_bracket_digit_tilde(self):
        """Handle ESC [ 9 ~ (where 9 was a digit)."""
        self.state = self.ESEQ_NONE
        if self.esc_seq == '3':
            return self.delete()
        if self.esc_seq == '2':
            return self.insert();
        if self.esc_seq == '1' or self.esc_seq == '7':
            return self.home()
        if self.esc_seq == '4' or self.esc_seq == '8':
            return self.end()
        self.log.error("Unknown ESC [ '%s' ~" % self.esc_seq)

    def esc_bracket_digit_typed_char(self, char):
        """We've previously received ESC [ digit."""
        if (char >= b'0' and char <= b'9') or char == b';':
            self.esc_seq += chr(ord(char))
            return
        self.log.error("Unknown ESC [ '%s' '%c' 0x%02x" %
                       (self.esc_seq, printable(char), ord(char)))

    def esc_bracket_typed_char(self, char):
        """Unrecognized ESC [ sequence."""
        if char >= b'0' and char <= b'9':
            self.esc_seq = chr(ord(char))
            self.state = self.ESEQ_ESC_BRACKET_DIGIT
        else:
            self.log.error("Unknown ESC [ '%c' 0x%02x" % (printable(char), ord(char)))
            self.state = self.ESEQ_NONE

    def esc_typed_char(self, char):
        """Unrecognized ESC sequence."""
        self.log.error("Unknown ESC '%c' 0x%02x" % (printable(char), ord(char)))
        self.state = self.ESEQ_NONE

    def esc_O(self):
        self.state = self.ESEQ_ESC_O

    def esc_O_typed_char(self, char):
        """Unrecognized ESC O sequence."""
        self.log.error("Unknown ESC O '%c' 0x%02x" % (printable(char), ord(char)))
        self.state = self.ESEQ_NONE

    def home(self):
        """Moves the cursor to the start of the line."""
        self.caret = 0
        self.state = self.ESEQ_NONE

    def insert(self):
        """Toggles between insert and overwrite mode."""
        self.overwrite = not self.overwrite

    def invalidate(self, from_pos, to_pos):
        from_col = from_pos - self.line_start
        to_col = to_pos - self.line_start
        if self.inval_start == -1:
            self.inval_start = from_col
            self.inval_end = to_col
        else:
            self.inval_start = min(from_col, self.inval_start)
            self.inval_end = max(to_col, self.inval_end)
        self.log.debug('invalidate(%d, %d) inval %d-%d' %
                       (from_pos, to_pos, self.inval_start, self.inval_end))   

    def left_arrow(self):
        if self.caret > 0:
            self.caret -= 1
        self.state = self.ESEQ_NONE

    def line_complete(self):
        """Final processing."""
        return self.line

    def right_arrow(self):
        if self.caret < len(self.line):
            self.caret += 1
        self.state = self.ESEQ_NONE

    def typed_char(self, char):
        """Handles regular characters."""
        if self.overwrite:
            self.line = self.line[:self.caret] + chr(ord(char)) + self.line[self.caret + 1:]
        else:
            self.line = self.line[:self.caret] + chr(ord(char)) + self.line[self.caret:]
        self.log.debug('typed_char: len(self.line) = %d' % len(self.line))
        self.invalidate(self.caret, len(self.line))
        self.caret += 1

    def up_arrow(self):
        self.state = self.ESEQ_NONE

    def process_line(self, line):
        """Primarily for testing. This basically runs a bunch of characters
           through process_char followed by a CR.
        """
        self.process_str(line)
        return self.process_char(CR)

    def process_str(self, string):
        """Calls process_char for each character in the string."""
        for byte in string:
            self.process_char(bytes((byte,)))

    def process_char(self, char):
        """Processes a single character of intput."""
        self.prev_line_len = len(self.line)
        if char in self.state:
            action = self.state[char]
            args = ()
        else:
            action = self.state[None]
            args = (char,)
        self.log.debug("process_char '%c' 0x%02x - Action %-20s caret = %2d line = %s esc_seq = '%s' (before)" %
                       (printable(char), ord(char), action.__name__,
                        self.caret, repr(self.line), self.esc_seq))
        result = action(*args)
        self.log.debug("process_char '%c' 0x%02x - Action %-20s caret = %2d line = %s esc_seq = '%s' (after)" %
                       (printable(char), ord(char), action.__name__,
                        self.caret, repr(self.line), self.esc_seq))
        self.redraw()
        if result is not None:
            self.write('\r\n')
            self.line = ''
            self.caret = 0
        self.write_queue.process()
        return result

    def redraw(self):
        max_width = self.columns - self.prompt_width
        if max_width < 0:
            max_width = len(self.line)

        #max_width = min(10, max_width)

        # Make sure that the cursor stays in the visible area and scroll the
        # contents to make sure it does

        if self.caret < self.line_start:
            self.line_start = self.caret
            self.invalidate(self.line_start, self.line_start + max_width)
        if self.caret - self.line_start > max_width:
            self.line_start = self.caret - max_width
            self.invalidate(self.line_start, self.line_start + max_width)
        self.inval_end = min(max_width, self.inval_end)

        self.log.debug('redraw: inval %d-%d cursor: %d line_start: %d caret: %d max_width: %d prompt_width: %d columns: %d' %
                       (self.inval_start, self.inval_end, self.cursor_col, self.line_start, self.caret, max_width, self.prompt_width, self.columns))

        if self.inval_start < self.inval_end:
            self.move_cursor_to_col(self.inval_start)
            line_end_col = len(self.line) - self.line_start
            write_cols = min(line_end_col - self.inval_start,
                             self.inval_end - self.inval_start)
            start_idx = self.inval_start + self.line_start
            self.log.debug('redraw: write_cols = %d' % write_cols)
            self.write(self.line[start_idx:start_idx + write_cols])
            self.cursor_col += write_cols
            self.inval_start += write_cols
            if self.inval_start < self.inval_end:
                self.erase_line_from_cursor()
            self.inval_start = -1
            self.inval_end = -1
        self.log.debug('redraw: self.caret = %d self.line_start = %d' % (self.caret, self.line_start))
        self.move_cursor_to_col(self.caret - self.line_start)

    def erase_line_from_cursor(self):
        self.write('\x1b[K')

    def move_cursor_to_col(self, col):
        self.log.debug('move_cursor_to_col(%d)' % col)
        if col < self.cursor_col:
            cols = self.cursor_col - col
            if cols <= 4:
                self.write('\b\b\b\b'[:cols])
            else:
                self.write('\x1b[%uD' % cols)
            self.cursor_col -= cols
        elif col > self.cursor_col:
            cols = col - self.cursor_col
            self.write('\x1b[%uC' % cols)
            self.cursor_col += cols

    def write(self, string):
        if TESTING:
            return
        self.log.debug('queued write(' + repr(string) + ')')
        self.write_queue.write(string)

