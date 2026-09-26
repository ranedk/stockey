//go:build mage

// Lifecycle automation for systrader's own long-running services, invoked as
// `mage <target>` (installed via `go install github.com/magefile/mage@latest`).
// Started 2026-08-29 for cmd/api (the systrader API serving screener/'s
// stage-analysis page) -- the same problem stockey solves with cron +
// all_fundamentals_api.sh's own health-check-then-start script, done in Go
// instead of bash/Python since this repo has no Python and keeping the
// service-management logic in the same language as the service itself avoids
// a second toolchain just for this.
package main

import (
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/magefile/mage/mg"
	"github.com/magefile/mage/sh"
)

const (
	apiBinPath = "bin/api"
	apiPidFile = "run/api.pid"
	apiLogFile = "logs/api.log"
)

// Api groups every systrader-API lifecycle target: mage api:ensure, api:stop,
// api:restart, api:build.
type Api mg.Namespace

func apiHealthURL() string {
	port := os.Getenv("API_PORT")
	if port == "" {
		port = "8090"
	}
	return fmt.Sprintf("http://127.0.0.1:%s/api/health", port)
}

func apiIsHealthy() bool {
	client := http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(apiHealthURL())
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// Build compiles cmd/api to bin/api. A separate target (not folded into
// Ensure) so `mage build` alone is a quick compile-check in CI/dev without
// touching any running process.
func Build() error {
	if err := os.MkdirAll("bin", 0o755); err != nil {
		return err
	}
	return sh.RunV("go", "build", "-o", apiBinPath, "./cmd/api")
}

// Ensure starts the API server if it isn't already answering /api/health --
// a no-op otherwise. Meant to be cron-driven (see scripts/ensure_api_alive.sh)
// every few minutes, flock-guarded the same way scripts/sync_from_stockey.sh's
// own crontab entry already is, so a crashed server self-heals instead of
// sitting silent until a human notices.
func (Api) Ensure() error {
	if apiIsHealthy() {
		fmt.Println("[api:ensure] already healthy, nothing to do")
		return nil
	}
	return startAPI()
}

// Restart force-restarts the server even if it's currently healthy -- for
// after a code change. The server does NOT hot-reload; running code and
// committed code silently diverging until someone thinks to restart is
// exactly the bug that left stockey's fundamentals API serving 15-day-stale
// code (2026-08-29 incident, this same session). Run this after every
// `go build`-affecting change to cmd/api, internal/stageapi, or internal/stage.
func (Api) Restart() error {
	_ = (Api{}).Stop()
	return startAPI()
}

// Stop sends SIGTERM to the tracked API process (via its pidfile) and
// removes the pidfile. A no-op, not an error, if nothing is tracked.
func (Api) Stop() error {
	pid, err := readAPIPid()
	if err != nil {
		fmt.Println("[api:stop] no pidfile, nothing to stop")
		return nil
	}
	proc, err := os.FindProcess(pid)
	if err == nil {
		if err := proc.Signal(syscall.SIGTERM); err != nil {
			fmt.Printf("[api:stop] pid %d not running (%v)\n", pid, err)
		} else {
			fmt.Printf("[api:stop] sent SIGTERM to pid %d\n", pid)
		}
	}
	_ = os.Remove(apiPidFile)
	return nil
}

func startAPI() error {
	mg.Deps(Build)

	if err := os.MkdirAll("run", 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll("logs", 0o755); err != nil {
		return err
	}
	logFile, err := os.OpenFile(apiLogFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer logFile.Close()

	bin, err := filepath.Abs(apiBinPath)
	if err != nil {
		return err
	}
	cmd := exec.Command(bin)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	// Setsid detaches the child into its own session so it outlives this mage
	// invocation -- mage exits right after Start(), same relationship cron has
	// to all_fundamentals_api.sh's exec'd uvicorn process on the stockey side.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	if err := os.WriteFile(apiPidFile, []byte(strconv.Itoa(cmd.Process.Pid)), 0o644); err != nil {
		return err
	}
	fmt.Printf("[api] started pid %d, waiting for health...\n", cmd.Process.Pid)

	for i := 0; i < 15; i++ {
		time.Sleep(500 * time.Millisecond)
		if apiIsHealthy() {
			fmt.Println("[api] healthy")
			return nil
		}
	}
	return fmt.Errorf("api did not become healthy within 7.5s of starting -- check %s", apiLogFile)
}

func readAPIPid() (int, error) {
	data, err := os.ReadFile(apiPidFile)
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(strings.TrimSpace(string(data)))
}
