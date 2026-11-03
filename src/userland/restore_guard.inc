/* Bounded restore gate for the MIT-licensed HakiOS init (HakiOS authors, 2025-2026).
 * Inserted only into the pinned upstream source by prepare-init.py.
 * Darwin WNOHANG=1, SIGKILL=9; SYS_kill comes from that source's libmini.
 */
static int
restore_var(void)
{
	const char *wanted = boot_arg("var_store");
	char *argv[3];
	int status = 0;
	pid_t pid;

	if (wanted == NULL || strcmp(wanted, "1") != 0) {
		return 1;
	}
	printf("\n-- restoring the previous data volume (60 second budget) --\n");
	argv[0] = (char *)in_image("/bin/var_store");
	argv[1] = (char *)"load";
	argv[2] = NULL;
	pid = fork();
	if (pid == 0) {
		execve(argv[0], argv, environ);
		printf("  %s: %s\n", argv[0], strerror(errno));
		_exit(127);
	}
	if (pid < 0) {
		printf("init: cannot start data restore\n");
		return 0;
	}
	/* Nonblocking waits: a stuck restore must not indefinitely hold up process 1.
	 * A finite poll budget also works when wall time is not initialized yet. */
	for (unsigned poll = 0; poll < 600; poll++) {
		pid_t result = wait4(pid, &status, 1, NULL);
		if (result == pid) {
			if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
				return 1;
			}
			printf("init: data restore failed (wait status %d)\n", status);
			return 0;
		}
		if (result < 0 && errno != 4) { /* Darwin EINTR=4; retry remains bounded */
			printf("init: cannot observe data restore\n");
			break;
		}
		usleep(100000);
	}
	printf("init: data restore exceeded its wait budget or failed; stopping child\n");
	(void)mini_syscall(SYS_kill, pid, 9, 0, 0, 0, 0);
	/* Reaping must also be bounded, even if a kernel I/O request cannot be killed. */
	for (unsigned poll = 0; poll < 20; poll++) {
		if (wait4(pid, &status, 1, NULL) == pid) {
			break;
		}
		usleep(100000);
	}
	return 0;
}
