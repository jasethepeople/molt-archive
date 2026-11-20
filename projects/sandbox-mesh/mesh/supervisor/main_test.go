package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestAgentHelperProcess(t *testing.T) {
	if os.Getenv("SANDBOX_MESH_AGENT_HELPER") != "1" {
		return
	}
	separator := -1
	for index, argument := range os.Args {
		if argument == "--" {
			separator = index
			break
		}
	}
	if separator < 0 || len(os.Args)-separator != 4 || os.Args[separator+1] != "--agent" {
		os.Exit(64)
	}
	interval, err := time.ParseDuration(os.Args[separator+3] + "s")
	if err != nil || runAgent(context.Background(), os.Args[separator+2], interval, os.Stdout, time.Now) != nil {
		os.Exit(1)
	}
	os.Exit(0)
}

func TestValidJobID(t *testing.T) {
	if !validJobID("0123456789ab") {
		t.Fatal("valid lower-case job id was rejected")
	}
	for _, value := range []string{"", "0123456789aB", "0123456789abc", "../../etc/passwd"} {
		if validJobID(value) {
			t.Fatalf("invalid job id %q was accepted", value)
		}
	}
}

func TestAgentEnvironmentIsScopedAndAppCannotReadIt(t *testing.T) {
	token := strings.Repeat("x", 43)
	task := "heartbeat only"
	if got := agentEnvironment(token, task); len(got) != 2 || got[0] != "SANDBOX_MESH_AGENT_TOKEN="+token || got[1] != "SANDBOX_MESH_AGENT_TASK="+task {
		t.Fatalf("unexpected agent environment: %#v", got)
	}
	if agentEnvironment("short", task) != nil || agentEnvironment(token, " ") != nil {
		t.Fatal("invalid per-job credentials were accepted")
	}
	got := applicationEnvironment([]string{"PATH=/usr/bin", "SANDBOX_MESH_AGENT_TOKEN=" + token, "SANDBOX_MESH_AGENT_TASK=" + task, "APP_MODE=prod"})
	if strings.Join(got, "|") != "PATH=/usr/bin|APP_MODE=prod" {
		t.Fatalf("agent credentials leaked into app environment: %#v", got)
	}
}

func TestAgentEmitsImmediateAndPeriodicHeartbeats(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var output bytes.Buffer
	done := make(chan error, 1)
	go func() { done <- runAgent(ctx, "0123456789ab", time.Second, &output, time.Now) }()
	time.Sleep(1100 * time.Millisecond)
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("agent returned an error: %v", err)
	}
	lines := strings.Fields(output.String())
	if len(lines) < 6 || lines[0] != "agent-heartbeat" || lines[1] != "0123456789ab" {
		t.Fatalf("expected at least two structured heartbeats, got %q", output.String())
	}
}

func TestSupervisorKillsAgentWhenAppExits(t *testing.T) {
	t.Setenv("SANDBOX_MESH_AGENT_TOKEN", strings.Repeat("x", 43))
	t.Setenv("SANDBOX_MESH_AGENT_TASK", "heartbeat only")
	status := runSupervisor("0123456789ab", 1, []string{"/bin/sh", "-c", "sleep 0.1; exit 7"})
	if status != 1 {
		t.Fatalf("expected a supervised app exit to fail the container, got %d", status)
	}
}

func TestAgentCrashTearsDownOnlyItsOwnApp(t *testing.T) {
	t.Setenv("SANDBOX_MESH_AGENT_TOKEN", strings.Repeat("x", 43))
	t.Setenv("SANDBOX_MESH_AGENT_TASK", "heartbeat only")
	pidDir := t.TempDir()
	t.Setenv("SANDBOX_MESH_PID_DIR", pidDir)
	jobID := "abcdef123456"
	done := make(chan int, 1)
	go func() { done <- runSupervisor(jobID, 1, []string{"sleep", "30"}) }()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(filepath.Join(pidDir, "sandbox-mesh-"+jobID+"-agent.pid")); err == nil {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := killSandboxProcess("agent", jobID, pidDir); err != nil {
		t.Fatalf("test hook could not signal the agent: %v", err)
	}
	select {
	case status := <-done:
		if status != 1 {
			t.Fatalf("agent exit should fail the supervised container, got %d", status)
		}
	case <-time.After(4 * time.Second):
		t.Fatal("supervisor did not stop the app after agent exit")
	}
	if _, err := os.Stat(filepath.Join(pidDir, "sandbox-mesh-"+jobID+"-app.pid")); !os.IsNotExist(err) {
		t.Fatal("app PID marker was not removed after teardown")
	}
}

func TestSupervisorRejectsEmptyAppCommand(t *testing.T) {
	if status := runSupervisor("abcdef123456", 5, nil); status != 64 {
		t.Fatalf("expected invalid command status 64, got %d", status)
	}
}
