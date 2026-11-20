package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type processExit struct {
	role string
	cmd  *exec.Cmd
	err  error
}

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "--agent":
			if len(os.Args) != 4 {
				os.Exit(64)
			}
			interval, err := strconv.Atoi(os.Args[3])
			if err != nil || interval < 1 || interval > 60 {
				os.Exit(64)
			}
			if err := runAgent(context.Background(), os.Args[2], time.Duration(interval)*time.Second, os.Stdout, time.Now); err != nil {
				fmt.Fprintln(os.Stderr, "sandbox-agent exited")
				os.Exit(1)
			}
			return
		case "--kill-app", "--kill-agent":
			if len(os.Args) != 3 || !validJobID(os.Args[2]) {
				os.Exit(64)
			}
			if err := killSandboxProcess(os.Args[1][7:], os.Args[2], "/tmp"); err != nil {
				fmt.Fprintln(os.Stderr, "sandbox process could not be signaled")
				os.Exit(1)
			}
			return
		case "--supervise":
			if len(os.Args) < 6 || os.Args[4] != "--" || !validJobID(os.Args[2]) {
				os.Exit(64)
			}
			interval, err := strconv.Atoi(os.Args[3])
			if err != nil || interval < 1 || interval > 60 {
				os.Exit(64)
			}
			os.Exit(runSupervisor(os.Args[2], interval, os.Args[5:]))
		}
	}
	fmt.Fprintln(os.Stderr, "sandbox-supervisor requires a supported mode")
	os.Exit(64)
}

func agentEnvironment(token, task string) []string {
	if len(token) < 32 || strings.TrimSpace(task) == "" {
		return nil
	}
	return []string{"SANDBOX_MESH_AGENT_TOKEN=" + token, "SANDBOX_MESH_AGENT_TASK=" + task}
}

func applicationEnvironment(environment []string) []string {
	filtered := make([]string, 0, len(environment))
	for _, variable := range environment {
		if strings.HasPrefix(variable, "SANDBOX_MESH_AGENT_") {
			continue
		}
		filtered = append(filtered, variable)
	}
	return filtered
}

func validJobID(value string) bool {
	if len(value) != 12 {
		return false
	}
	for _, char := range value {
		if !(char >= '0' && char <= '9') && !(char >= 'a' && char <= 'f') {
			return false
		}
	}
	return true
}

func runAgent(ctx context.Context, jobID string, interval time.Duration, output io.Writer, now func() time.Time) error {
	if !validJobID(jobID) || interval <= 0 {
		return errors.New("invalid agent configuration")
	}
	writer := bufio.NewWriter(output)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	write := func() error {
		if _, err := fmt.Fprintf(writer, "agent-heartbeat %s %d\n", jobID, now().Unix()); err != nil {
			return err
		}
		return writer.Flush()
	}
	if err := write(); err != nil {
		return err
	}
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if err := write(); err != nil {
				return err
			}
		}
	}
}

func runSupervisor(jobID string, interval int, appArgv []string) int {
	if !validJobID(jobID) || interval < 1 || interval > 60 || len(appArgv) == 0 || appArgv[0] == "" {
		fmt.Fprintln(os.Stderr, "sandbox-supervisor received an invalid app command")
		return 64
	}
	executable, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, "sandbox-supervisor could not resolve its executable")
		return 70
	}
	agentEnv := agentEnvironment(os.Getenv("SANDBOX_MESH_AGENT_TOKEN"), os.Getenv("SANDBOX_MESH_AGENT_TASK"))
	if len(agentEnv) != 2 {
		fmt.Fprintf(os.Stderr, "sandbox-supervisor %s agent-start-failed\\n", jobID)
		return 70
	}
	agentArgs := []string{"--agent", jobID, strconv.Itoa(interval)}
	agent := exec.Command(executable, agentArgs...)
	agent.Env = agentEnv
	if strings.HasSuffix(executable, ".test") {
		testArgs := append([]string{"-test.run=TestAgentHelperProcess", "--"}, agentArgs...)
		agent = exec.Command(executable, testArgs...)
		agent.Env = append(agentEnv, "SANDBOX_MESH_AGENT_HELPER=1")
	}
	agent.Stdout, agent.Stderr = os.Stdout, os.Stderr
	agent.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	app := exec.Command(appArgv[0], appArgv[1:]...)
	app.Env = applicationEnvironment(os.Environ())
	app.Stdout, app.Stderr = os.Stdout, os.Stderr
	app.Stdin = os.Stdin
	app.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	pidDir := os.Getenv("SANDBOX_MESH_PID_DIR")
	if pidDir == "" {
		pidDir = "/tmp"
	}
	if err := os.MkdirAll(pidDir, 0o700); err != nil {
		fmt.Fprintln(os.Stderr, "sandbox-supervisor could not prepare process tracking")
		return 70
	}
	if err := agent.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "sandbox-supervisor %s agent-start-failed\n", jobID)
		return 70
	}
	if err := writePID(pidDir, jobID, "agent", agent.Process.Pid); err != nil {
		terminateGroup(agent.Process.Pid)
		_, _ = agent.Process.Wait()
		fmt.Fprintf(os.Stderr, "sandbox-supervisor %s agent-start-failed\n", jobID)
		return 70
	}
	if err := app.Start(); err != nil {
		terminateGroup(agent.Process.Pid)
		_, _ = agent.Process.Wait()
		removePID(pidDir, jobID, "agent")
		fmt.Fprintf(os.Stderr, "sandbox-supervisor %s app-start-failed\n", jobID)
		return 70
	}
	if err := writePID(pidDir, jobID, "app", app.Process.Pid); err != nil {
		terminateGroup(app.Process.Pid)
		terminateGroup(agent.Process.Pid)
		_, _ = app.Process.Wait()
		_, _ = agent.Process.Wait()
		removePID(pidDir, jobID, "agent")
		fmt.Fprintf(os.Stderr, "sandbox-supervisor %s app-start-failed\n", jobID)
		return 70
	}

	exits := make(chan processExit, 2)
	go func() { exits <- processExit{role: "agent", cmd: agent, err: agent.Wait()} }()
	go func() { exits <- processExit{role: "app", cmd: app, err: app.Wait()} }()
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT, syscall.SIGQUIT)
	defer signal.Stop(signals)

	select {
	case received := <-signals:
		fmt.Fprintf(os.Stderr, "sandbox-supervisor %s stopping signal=%s\n", jobID, received)
		terminateGroup(agent.Process.Pid)
		terminateGroup(app.Process.Pid)
		waitForExit(exits, 2)
		waitForExit(exits, 2)
		removePID(pidDir, jobID, "agent")
		removePID(pidDir, jobID, "app")
		return 0
	case ended := <-exits:
		status := exitStatus(ended.err)
		fmt.Fprintf(os.Stderr, "sandbox-supervisor %s %s-exited status=%d\n", jobID, ended.role, status)
		other := app
		otherRole := "app"
		if ended.role == "app" {
			other, otherRole = agent, "agent"
		}
		terminateGroup(other.Process.Pid)
		remaining := waitForExit(exits, 2)
		if remaining.cmd != nil {
			removePID(pidDir, jobID, remaining.role)
			fmt.Fprintf(os.Stderr, "sandbox-supervisor %s %s-stopped-after-%s\n", jobID, otherRole, ended.role)
		}
		removePID(pidDir, jobID, "agent")
		removePID(pidDir, jobID, "app")
		return 1
	}
}

func waitForExit(exits <-chan processExit, seconds int) processExit {
	select {
	case ended := <-exits:
		return ended
	case <-time.After(time.Duration(seconds) * time.Second):
		return processExit{}
	}
}

func exitStatus(err error) int {
	if err == nil {
		return 0
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode()
	}
	return 255
}

func terminateGroup(pid int) {
	if pid <= 0 {
		return
	}
	_ = syscall.Kill(-pid, syscall.SIGTERM)
	timer := time.NewTimer(2 * time.Second)
	defer timer.Stop()
	for {
		if err := syscall.Kill(-pid, 0); err != nil {
			return
		}
		select {
		case <-timer.C:
			_ = syscall.Kill(-pid, syscall.SIGKILL)
			return
		case <-time.After(50 * time.Millisecond):
		}
	}
}

func writePID(directory, jobID, role string, pid int) error {
	path := filepath.Join(directory, "sandbox-mesh-"+jobID+"-"+role+".pid")
	return os.WriteFile(path, []byte(strconv.Itoa(pid)+"\n"), 0o600)
}

func removePID(directory, jobID, role string) {
	_ = os.Remove(filepath.Join(directory, "sandbox-mesh-"+jobID+"-"+role+".pid"))
}

func killSandboxProcess(role, jobID, directory string) error {
	if (role != "app" && role != "agent") || !validJobID(jobID) {
		return errors.New("invalid process selector")
	}
	path := filepath.Join(directory, "sandbox-mesh-"+jobID+"-"+role+".pid")
	content, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(content)))
	if err != nil || pid <= 1 {
		return errors.New("invalid process id")
	}
	return syscall.Kill(pid, syscall.SIGKILL)
}
