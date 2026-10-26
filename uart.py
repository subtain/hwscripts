import sys
import os
import threading
import signal
import codecs
from datetime import datetime

import serial


# ------------------------------------------------------------
# Command-line arguments
# ------------------------------------------------------------
if len(sys.argv) < 3:
    print("Usage: python uart.py COM35 115200")
    sys.exit(1)

port = sys.argv[1]
baud = int(sys.argv[2])

ser = serial.Serial(
    port=port,
    baudrate=baud,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=0.1
)

stop_event = threading.Event()
serial_lock = threading.Lock()
print_lock = threading.Lock()

# Default settings
timestamp_enabled = True
log_file = None
log_file_name = None

# Decode UART data safely, including partial UTF-8 characters.
decoder = codecs.getincrementaldecoder("utf-8")("replace")

line_buffer = ""
previous_char_was_cr = False
reader_thread = None


# ------------------------------------------------------------
# Display and logging functions
# ------------------------------------------------------------
def local_timestamp():
    """Return PC local time with milliseconds."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def print_banner():
    """Print UART console header."""
    timestamp_status = "ON" if timestamp_enabled else "OFF"
    log_status = "OFF"

    if log_file is not None:
        log_status = "ON ({})".format(log_file_name)

    print("Connected: {} at {} baud".format(port, ser.baudrate))
    print("Commands:")
    print(" /baud 921600 Change UART baud rate")
    print(" /timestamp on Enable local timestamps")
    print(" /timestamp off Disable local timestamps")
    print(" /timestamp Show timestamp status")
    print(" /log on Start logging to automatic file")
    print(" /log on myfile.txt Start logging to selected file")
    print(" /log off Stop logging")
    print(" /log Show logging status")
    print(" /clear Clear output and redraw this header")
    print(" /quit Exit the program")
    print("Press Ctrl+C to exit.")
    print("Timestamp: {}".format(timestamp_status))
    print("Logging: {}".format(log_status))
    print("")


def clear_screen_and_show_banner():
    """
    Clear the visible Git Bash terminal and redraw the header.

    'cls' is more reliable on Windows/Git Bash than ANSI clear codes
    on some terminal configurations.
    """
    with print_lock:
        os.system("cls")

        # Also attempt to clear scrollback for ANSI-capable terminals.
        sys.stdout.write("\x1b[3J\x1b[H")
        sys.stdout.flush()

        print_banner()
        sys.stdout.flush()


def format_uart_line(text):
    """Create the UART line as displayed and optionally logged."""
    if timestamp_enabled:
        return "[{}] {}".format(local_timestamp(), text)

    return text


def write_log_line(text):
    """Write one received UART line to log file, if logging is enabled."""
    global log_file

    if log_file is None:
        return

    try:
        log_file.write(text + "\n")
        log_file.flush()

    except (OSError, IOError) as error:
        print("\n[{}] Log write error: {}".format(local_timestamp(), error))

        try:
            log_file.close()
        except (OSError, IOError):
            pass

        log_file = None


def print_uart_line(text):
    """Display one received UART line and save it if logging is enabled."""
    formatted_line = format_uart_line(text)

    with print_lock:
        print(formatted_line)
        sys.stdout.flush()

        # Log only received UART output, not local commands/status messages.
        write_log_line(formatted_line)


def print_status(text):
    """Display a local script status message."""
    with print_lock:
        print("\n[{}] {}".format(local_timestamp(), text))
        sys.stdout.flush()


def make_default_log_name():
    """Create a Windows-safe default UART log file name."""
    safe_port = port.replace(":", "_").replace("\\", "_").replace("/", "_")
    time_text = datetime.now().strftime("%Y%m%d_%H%M%S")

    return "uart_{}_{}.log".format(safe_port, time_text)


def start_logging(file_name=None):
    """Open a log file and enable logging."""
    global log_file
    global log_file_name

    if log_file is not None:
        print_status("Logging is already ON: {}".format(log_file_name))
        return

    if not file_name:
        file_name = make_default_log_name()

    try:
        log_file = open(file_name, "a", encoding="utf-8")
        log_file_name = os.path.abspath(file_name)

        log_file.write("\n")
        log_file.write("============================================================\n")
        log_file.write("UART log started: {}\n".format(local_timestamp()))
        log_file.write("Port: {} Baud: {}\n".format(port, ser.baudrate))
        log_file.write("============================================================\n")
        log_file.flush()

        print_status("Logging started: {}".format(log_file_name))

    except (OSError, IOError) as error:
        log_file = None
        log_file_name = None
        print_status("Cannot open log file: {}".format(error))


def stop_logging():
    """Stop logging and close the current log file."""
    global log_file
    global log_file_name

    if log_file is None:
        print_status("Logging is already OFF")
        return

    old_log_name = log_file_name

    try:
        log_file.write("UART log stopped: {}\n".format(local_timestamp()))
        log_file.close()
    except (OSError, IOError):
        pass

    log_file = None
    log_file_name = None

    print_status("Logging stopped: {}".format(old_log_name))


# ------------------------------------------------------------
# UART receive processing
# ------------------------------------------------------------
def process_uart_text(text):
    """
    Treat CR, LF, and CRLF as one line ending.

    Handles firmware output using:
      - \\n
      - \\r
      - \\r\\n
    """
    global line_buffer
    global previous_char_was_cr

    for char in text:
        if char == "\r":
            print_uart_line(line_buffer)
            line_buffer = ""
            previous_char_was_cr = True

        elif char == "\n":
            # A CRLF pair must create only one line break.
            if not previous_char_was_cr:
                print_uart_line(line_buffer)
                line_buffer = ""

            previous_char_was_cr = False

        else:
            line_buffer += char
            previous_char_was_cr = False


def uart_reader():
    """Read and display UART data continuously."""
    while not stop_event.is_set():
        try:
            with serial_lock:
                data = ser.read(1024)

            if data:
                decoded_text = decoder.decode(data)
                process_uart_text(decoded_text)

        except serial.SerialException as error:
            if not stop_event.is_set():
                print_status("UART read error: {}".format(error))
            break


# ------------------------------------------------------------
# Ctrl+C support
# ------------------------------------------------------------
def stop_program(signum=None, frame=None):
    """Handle Ctrl+C."""
    stop_event.set()


signal.signal(signal.SIGINT, stop_program)


# ------------------------------------------------------------
# Startup
# ------------------------------------------------------------
clear_screen_and_show_banner()

reader_thread = threading.Thread(target=uart_reader, daemon=True)
reader_thread.start()


# ------------------------------------------------------------
# Interactive command loop
# ------------------------------------------------------------
try:
    while not stop_event.is_set():
        try:
            command = input()
        except EOFError:
            break
        except KeyboardInterrupt:
            break

        command_stripped = command.strip()
        command_lower = command_stripped.lower()

        # /clear
        if command_lower == "/clear":
            clear_screen_and_show_banner()

        # /baud 921600
        elif command_lower.startswith("/baud "):
            try:
                new_baud = int(command_stripped.split()[1])

                if new_baud <= 0:
                    raise ValueError

                with serial_lock:
                    ser.baudrate = new_baud

                print_status("Baud rate changed to {}".format(new_baud))

            except (ValueError, IndexError):
                print_status("Usage: /baud <rate> Example: /baud 921600")

        # /timestamp on
        elif command_lower == "/timestamp on":
            timestamp_enabled = True
            print_status("Local timestamps enabled")

        # /timestamp off
        elif command_lower == "/timestamp off":
            timestamp_enabled = False
            print_status("Local timestamps disabled")

        # /timestamp
        elif command_lower == "/timestamp":
            status = "ON" if timestamp_enabled else "OFF"
            print_status("Local timestamps are {}".format(status))

        # /log on
        # /log on my_log_file.txt
        elif command_lower.startswith("/log on"):
            parts = command_stripped.split(None, 2)

            if len(parts) >= 3:
                start_logging(parts[2])
            else:
                start_logging()

        # /log off
        elif command_lower == "/log off":
            stop_logging()

        # /log
        elif command_lower == "/log":
            if log_file is None:
                print_status("Logging is OFF")
            else:
                print_status("Logging is ON: {}".format(log_file_name))

        # /quit or /exit
        elif command_lower in ("/quit", "/exit"):
            break

        # Ignore blank line.
        elif command_stripped == "":
            continue

        # Send any normal entered text to UART.
        else:
            try:
                with serial_lock:
                    # Change "\r\n" to "\n" if the target requires LF only.
                    ser.write((command + "\r\n").encode("utf-8"))

            except serial.SerialException as error:
                print_status("UART write error: {}".format(error))
                break


# ------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------
finally:
    stop_event.set()

    # Display/log any final UART data not ending in CR/LF.
    if line_buffer:
        print_uart_line(line_buffer)

    print_status("Closing UART...")

    try:
        if ser.is_open:
            ser.close()
    except serial.SerialException:
        pass

    if reader_thread is not None:
        reader_thread.join(timeout=1.0)

    if log_file is not None:
        stop_logging()

    with print_lock:
        print("Disconnected.")
        sys.stdout.flush()