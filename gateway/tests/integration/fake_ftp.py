"""
A tiny FTP server for the integration test (scripts/test-provider-infra-integration.sh): passive mode only, with the
data connection on a port the SERVER picks in 30000-30100 and a bogus address in the 227 reply, as a server behind a
NAT does. It serves one file, hello.txt, whose content names the data port it was sent on.
"""
import socket
import threading

CONTROL_PORT = 2121
DATA_PORTS = range(30000, 30101)


def serve_control(connection: socket.socket) -> None:
    stream = connection.makefile("rwb")
    reply = lambda text: (stream.write(text.encode() + b"\r\n"), stream.flush())  # noqa: E731
    reply("220 fake ftp")
    data_listener = None
    port = 0
    while line := stream.readline():
        command, _, argument = line.decode().strip().partition(" ")
        if command == "USER":
            reply("331 any password")
        elif command == "PASS":
            reply("230 welcome")
        elif command in ("TYPE", "OPTS"):
            reply("200 ok")
        elif command == "PASV":
            data_listener = socket.socket()
            for port in DATA_PORTS:
                try:
                    data_listener.bind(("0.0.0.0", port))
                    break
                except OSError:
                    continue
            data_listener.listen(1)
            reply(f"227 Entering Passive Mode (1,2,3,4,{port // 256},{port % 256})")
        elif command in ("NLST", "RETR"):
            reply("150 sending")
            data, _ = data_listener.accept()
            with data:
                data.sendall(b"hello.txt\n" if command == "NLST" else f"hello from data port {port}".encode())
            reply("226 done")
        elif command == "QUIT":
            reply("221 bye")
            return
        else:
            reply("502 no")


def main() -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", CONTROL_PORT))
    listener.listen(8)
    while True:
        connection, _ = listener.accept()
        threading.Thread(target=lambda c=connection: (serve_control(c), c.close()), daemon=True).start()


if __name__ == "__main__":
    main()
