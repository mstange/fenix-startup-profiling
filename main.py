#!/usr/bin/env python3
"""
Android Profile Automation

Automates the workflow described in MANUAL_WORKFLOW.md for creating merged profiles
from simpleperf and the Gecko Profiler.
"""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import toml

WARMUP_BACKGROUND_TABS = [
    "https://www.google.com/search?q=toronto+weather",
    "https://en.m.wikipedia.org/wiki/Anemone_hepatica",
    "https://www.temu.com",
    "https://www.espn.com/nfl/game/_/gameId/401671793/chiefs-falcons",
]

# Apps whose processes we kill before the test
BROWSER_PACKAGE_NAMES = [
    "org.mozilla.firefox",
    "org.mozilla.fenix",
    "org.mozilla.fenix.debug",
    "org.chromium.chrome",
    "com.android.chrome",
]

# PACKAGE_NAME will be replaced with the package name
GECKOVIEW_CONFIG = """env:
  PERF_SPEW_DIR: /storage/emulated/0/Android/data/PACKAGE_NAME/files
  IONPERF: func
  JIT_OPTION_emitInterpreterEntryTrampoline: true
  JIT_OPTION_enableICFramePointers: true
  JIT_OPTION_onlyInlineSelfHosted: true

#   MOZ_LOG: "ScriptPreloader:5,IndexedDB:5,mozStorage:5"

  MOZ_PROFILER_STARTUP: 1
  MOZ_PROFILER_STARTUP_NO_BASE: 1 # bug 1955125
  MOZ_PROFILER_STARTUP_INTERVAL: 500
  MOZ_PROFILER_STARTUP_FEATURES: nostacksampling,nomarkerstacks,screenshots,ipcmessages,java,cpu,markersallthreads,flows,fileio
  MOZ_PROFILER_STARTUP_FILTERS: GeckoMain,Compositor,Renderer,IPDL Background,*
"""

logger = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    """Setup logging configuration."""
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {level}")

    # Configure logging format
    formatter = logging.Formatter(fmt="%(levelname)s: %(message)s", datefmt="%H:%M:%S")

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.addHandler(handler)


def expand_path(path: str) -> str:
    # First expand user home directory (~) if present
    expanded = os.path.expanduser(path)

    # If it's now absolute, return it
    if os.path.isabs(expanded):
        return expanded

    # Treat path as being relative to this script file, and expand it into an absolute path.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, expanded)


def resolve_binary_path(path: str) -> str:
    """Resolve binary path, keeping simple names for shell resolution."""
    # Just expand ~ if present, let shell handle PATH resolution
    return os.path.expanduser(path)


class AndroidProfileAutomation:
    def __init__(
        self,
        config_path: str = "config.toml",
        device_id: str | None = None,
        with_warmup: bool = False,
        profile_warmup: bool = False,
        use_java: bool = False,
        package: str | None = None,
        url: str | None = None,
        duration: int | None = None,
        frequency: int | None = None,
        output_path: str | None = None,
    ) -> None:
        self.config = self._load_config(config_path)
        self.temp_dir: str | None = None
        self.device_id = device_id
        self.use_java = use_java
        self.with_warmup = with_warmup
        self.profile_warmup = profile_warmup
        self.debug_app_set = False  # Track if we've set debug app
        self.yaml_file_path = None

        if "duration" not in self.config:
            self.config["duration"] = 26 if profile_warmup else 6

        script_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.simpleperf_profile_path = str(
            script_dir / "out" / f"simpleperf-profile_{timestamp}.json.gz"
        )
        self.gecko_profile_path = str(script_dir / "out" / f"gecko-profile_{timestamp}.json.gz")

        # Determine output file path
        if output_path:
            # Use specified output path
            self.output_path = os.path.abspath(output_path)
            self.should_open_with_samply = False
        else:
            # Store to ./out/merged-profile-<DATE>-<TIME>.json.gz
            self.output_path = str(script_dir / "out" / f"merged-profile_{timestamp}.json.gz")
            self.should_open_with_samply = True

        # Apply CLI overrides
        if package:
            self.config["package_name"] = package
        if url:
            self.config["startup_url"] = url
        if duration:
            self.config["duration"] = duration
        if frequency:
            self.config["frequency"] = frequency

        # Resolve paths
        self.samply_binary: str = resolve_binary_path(self.config["samply_binary"])
        self.merge_script: str = expand_path(self.config["merge_script"])
        self.symbol_dirs: list[str] = [expand_path(d) for d in self.config["symbol_dirs"] if d]
        self.breakpad_symbol_dirs: list[str] = [
            expand_path(d) for d in self.config["breakpad_symbol_dirs"] if d
        ]
        self.breakpad_symbol_servers: list[str] = [
            s for s in self.config["breakpad_symbol_servers"] if s
        ]

        self.activity_name = "org.mozilla.fenix.IntentReceiverActivity"
        if ".chrome" in self.config["package_name"]:
            self.activity_name = "com.google.android.apps.chrome.IntentDispatcher"

        # Make out_dir relative to the script location, not the current working directory
        script_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        self.out_dir = script_dir / "out"

        # For convenience, create the out_dir
        os.makedirs(self.out_dir, exist_ok=True)

    def _load_config(self, config_path: str) -> dict[str, Any]:
        """Load configuration from TOML file."""
        try:
            with open(config_path) as f:
                config = toml.load(f)

            android_config = config["android_profiling"]

            # Normalize symbol configurations to arrays and make them optional
            self._normalize_symbol_config(android_config)

            return android_config  # type: ignore[no-any-return]
        except FileNotFoundError:
            print(f"Config file {config_path} not found!")
            sys.exit(1)
        except Exception as e:
            print(f"Error loading config: {e}")
            sys.exit(1)

    def _normalize_symbol_config(self, config: dict[str, Any]) -> None:
        """Convert symbol config to arrays and handle legacy format."""
        # Handle symbol directories
        if "symbol_dirs" in config:
            if isinstance(config["symbol_dirs"], str):
                config["symbol_dirs"] = [config["symbol_dirs"]]
        elif "symbol_dir" in config:
            # Legacy format
            config["symbol_dirs"] = [config["symbol_dir"]]
        else:
            config["symbol_dirs"] = []

        # Handle breakpad symbol servers
        if "breakpad_symbol_servers" in config:
            if isinstance(config["breakpad_symbol_servers"], str):
                config["breakpad_symbol_servers"] = [config["breakpad_symbol_servers"]]
        elif "breakpad_symbol_server" in config:
            # Legacy format
            config["breakpad_symbol_servers"] = [config["breakpad_symbol_server"]]
        else:
            config["breakpad_symbol_servers"] = []

        # Handle breakpad symbol directories
        if "breakpad_symbol_dirs" in config:
            if isinstance(config["breakpad_symbol_dirs"], str):
                config["breakpad_symbol_dirs"] = [config["breakpad_symbol_dirs"]]
        elif "breakpad_symbol_dir" in config:
            # Legacy format
            config["breakpad_symbol_dirs"] = [config["breakpad_symbol_dir"]]
        else:
            config["breakpad_symbol_dirs"] = []

    def _run_adb_command(
        self, cmd: str, capture_output: bool = False
    ) -> subprocess.CompletedProcess:
        """Run an adb command and return the result."""
        if self.device_id:
            full_cmd = f"adb -s {self.device_id} {cmd}"
        else:
            full_cmd = f"adb {cmd}"

        logger.debug(f"Running: {full_cmd}")

        if capture_output:
            result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0 and result.stderr:
                logger.debug(f"Command failed with stderr: {result.stderr.strip()}")
            return result
        else:
            # Capture output by default unless debug logging is enabled
            if logger.isEnabledFor(logging.DEBUG):
                return subprocess.run(full_cmd, shell=True)
            else:
                return subprocess.run(full_cmd, shell=True, capture_output=True, text=True)

    def _run_command(
        self, cmd: str, capture_output: bool = False, cwd: str | None = None
    ) -> subprocess.CompletedProcess:
        """Run a shell command and return the result."""
        logger.debug(f"Running: {cmd}")

        if capture_output:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
            if result.returncode != 0 and result.stderr:
                logger.debug(f"Command failed with stderr: {result.stderr.strip()}")
            return result
        else:
            # Capture output by default unless debug logging is enabled
            if logger.isEnabledFor(logging.DEBUG):
                return subprocess.run(cmd, shell=True, cwd=cwd)
            else:
                return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)

    def setup_temp_directory(self) -> None:
        """Create temporary directory for intermediate files."""
        self.temp_dir = tempfile.mkdtemp(prefix="android_profiling_")
        logger.debug(f"Using temporary directory: {self.temp_dir}")

    def cleanup_temp_directory(self) -> None:
        """Clean up temporary directory."""
        if self.temp_dir and os.path.exists(self.temp_dir):
            # shutil.rmtree(self.temp_dir)
            logger.debug(f"Cleaned up temporary directory: {self.temp_dir}")

    def cleanup_device_state(self) -> None:
        """Clean up device state (debug app settings)."""
        if self.debug_app_set:
            try:
                self._run_adb_command("shell am clear-debug-app")
                logger.debug("Cleared debug app state")
                self.debug_app_set = False
            except Exception as e:
                logger.warning(f"Failed to clear debug app state: {e}")

        if self.yaml_file_path:
            try:
                yaml_file_path = self.yaml_file_path
                self._run_adb_command(f'shell rm "{yaml_file_path}"')
                logger.debug("Deleted yaml file (gecko profiling)")
                self.debug_app_set = False
            except Exception as e:
                logger.warning(f"Failed delete yaml file (gecko profiling): {e}")

    def validate_environment(self) -> None:
        """Validate that all required tools and environment are available."""
        logger.info("Validating environment...")

        # Check samply binary and --presymbolicate option
        result = self._run_command(f'"{self.samply_binary}" import --help', capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to run samply. Check that '{self.config['samply_binary']}' is installed and in PATH."
            )

        if "--presymbolicate" not in result.stdout:
            raise RuntimeError(
                "samply import does not support --presymbolicate option. Please update samply."
            )

        logger.debug("samply binary found and supports --presymbolicate")

        # Check ADB connectivity
        self._validate_adb_connection()

        # Check device tools
        self._validate_device_tools()

        logger.info("Environment validation completed")

    def _validate_adb_connection(self) -> None:
        """Validate ADB connection and device availability."""
        # Check if adb is available
        result = self._run_command("adb version", capture_output=True)
        if result.returncode != 0:
            raise RuntimeError("adb command not found. Please install Android SDK Platform Tools.")

        # Get connected devices
        result = self._run_command("adb devices", capture_output=True)
        if result.returncode != 0:
            raise RuntimeError("Failed to run adb devices")

        lines = result.stdout.strip().split("\n")[1:]  # Skip header
        devices = [line.split("\t")[0] for line in lines if line.strip() and "device" in line]

        if not devices:
            raise RuntimeError(
                "No Android devices found. Please connect a device and enable USB debugging."
            )

        if len(devices) > 1 and not self.device_id:
            device_list = "\n".join([f"  - {device}" for device in devices])
            raise RuntimeError(
                f"Multiple devices found. Please specify a device ID with --device:\n{device_list}"
            )

        if self.device_id and self.device_id not in devices:
            device_list = "\n".join([f"  - {device}" for device in devices])
            raise RuntimeError(
                f"Device '{self.device_id}' not found. Available devices:\n{device_list}"
            )

        target_device = self.device_id if self.device_id else devices[0]
        logger.debug(f"Connected to device: {target_device}")

    def _validate_device_tools(self) -> None:
        """Validate that required tools are available on the device."""
        # Check if su works
        result = self._run_adb_command("shell su -c 'echo test'", capture_output=True)
        if result.returncode != 0 or "test" not in result.stdout:
            raise RuntimeError(
                "Root access (su) not available on device. Please root the device or grant root access."
            )

        logger.debug("Root access (su) available")

        # Check if simpleperf is available
        result = self._run_adb_command("shell ls /data/local/tmp/simpleperf", capture_output=True)
        if result.returncode != 0:
            # Try to find simpleperf in other locations
            result = self._run_adb_command("shell which simpleperf", capture_output=True)
            if result.returncode != 0:
                raise RuntimeError(
                    "simpleperf not found on device. Please install simpleperf binary to /data/local/tmp/simpleperf"
                )

        logger.debug("simpleperf available on device")

    def setup_gecko_profiling(self) -> None:
        """Create the GeckoView configuration file."""
        package_name = self.config["package_name"]

        config_filename = f"{package_name}-geckoview-config.yaml"
        config_path = os.path.join(self.temp_dir or "", config_filename)
        with open(config_path, "w") as f:
            f.write(GECKOVIEW_CONFIG.replace("PACKAGE_NAME", package_name))

        self._run_adb_command(f'push "{config_path}" /data/local/tmp/')
        self._run_adb_command(f"shell am set-debug-app --persistent {package_name}")
        self.debug_app_set = True
        self.yaml_file_path = f"/data/local/tmp/{config_filename}"

    def start_simpleperf_recording(self) -> subprocess.Popen[bytes]:
        """Start simpleperf recording in background."""
        duration = self.config["duration"]
        frequency = self.config["frequency"]

        # Choose callgraph option based on --java flag
        callgraph_option = "-g" if self.use_java else "--call-graph fp"

        cmd = (
            f'shell su -c "/data/local/tmp/simpleperf record {callgraph_option} '
            f"--duration {duration} "
            f" -f {frequency} "
            f'--trace-offcpu '
            f"-e cpu-clock "
            # f'-e binder:binder_ioctl -c 1 '
            f'-a -o /data/local/tmp/su-perf.data"'
        )

        logger.info(
            f"Starting simpleperf recording with {'DWARF' if self.use_java else 'framepointer'} unwinding..."
        )

        # Build full command
        if self.device_id:
            full_cmd = f"adb -s {self.device_id} {cmd}"
        else:
            full_cmd = f"adb {cmd}"

        logger.debug(f"Running background: {full_cmd}")

        # Capture output unless debug logging is enabled
        if logger.isEnabledFor(logging.DEBUG):
            proc = subprocess.Popen(full_cmd, shell=True)
        else:
            proc = subprocess.Popen(
                full_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

        # Give simpleperf time to start
        if self.use_java:
            time.sleep(4)
        else:
            time.sleep(2)
        return proc

    def trigger_app_startup(self) -> None:
        """Trigger the app startup sequence."""

        if self.profile_warmup:
            self.run_warmup_scenario()
            time.sleep(10)
        else:
            self.run_startup_scenario()

    def run_warmup_scenario(self) -> None:
        package_name = self.config["package_name"]
        activity_name = self.activity_name

        self._run_adb_command(f"shell am force-stop {package_name}")

        self._run_adb_command(
            f"shell pm clear {package_name}"
        )  # this will also kill any running processes
        time.sleep(3)
        self._run_adb_command(
            f"shell pm grant {package_name} android.permission.POST_NOTIFICATIONS"
        )
        self._run_adb_command(
            f"shell am start-activity -W -a android.intent.action.MAIN --ez performancetest true -n {package_name}/org.mozilla.fenix.App"
        )

        for website in WARMUP_BACKGROUND_TABS:
            website_cmd = (
                f'shell am start-activity -d "{website}" '
                f"-a android.intent.action.VIEW "
                f"{package_name}/{activity_name}"
            )

            self._run_adb_command(website_cmd)
            time.sleep(3)

    def run_startup_scenario(self) -> None:
        package_name = self.config["package_name"]
        activity_name = self.activity_name
        startup_url = self.config["startup_url"]

        self._run_adb_command(f"shell am force-stop {package_name}")
        self._run_adb_command(
            f"shell pm grant {package_name} android.permission.POST_NOTIFICATIONS"
        )

        startup_cmd = (
            f'shell am start-activity -d "{startup_url}" '
            f"-a android.intent.action.VIEW "
            f"{package_name}/{activity_name}"
        )
        # startup_cmd = (
        #     f"shell am start-activity -W -a android.intent.action.MAIN --ez performancetest true -n {package_name}/org.mozilla.fenix.App"
        # )

        self._run_adb_command(startup_cmd)

        duration = self.config["duration"]
        logger.info(
            f"App startup triggered. Waiting {duration} seconds for profiling to complete..."
        )
        time.sleep(duration)  # Wait for profiling duration to complete

    def capture_gecko_profile(self) -> None:
        """Capture the Gecko Profile."""

        package_name = self.config["package_name"]

        with open(self.gecko_profile_path, "wb") as output_file:
            result = subprocess.run(
                [
                    "adb",
                    "shell",
                    "content",
                    "read",
                    "--uri",
                    f"content://{package_name}.profiler/stop-and-upload",
                ],
                stdout=output_file,
                stderr=subprocess.PIPE,
                check=False,
            )

        if result.returncode != 0:
            print(
                f"Warning: Failed to stop profiler via content provider (exit code {result.returncode})"
            )

    def collect_simpleperf_data(self, simpleperf_proc: subprocess.Popen[bytes]) -> None:
        """Wait for simpleperf to complete and collect data."""
        logger.info("Waiting for simpleperf recording to complete...")
        simpleperf_proc.wait()

        perf_data_path = os.path.join(self.temp_dir or "", "su-perf.data")
        self._run_adb_command(f'pull /data/local/tmp/su-perf.data "{perf_data_path}"')

        package_name = self.config["package_name"]
        find_cmd = (
            f"shell find /storage/emulated/0/Android/data/{package_name}/files "
            f"'\\( -name jit-* -or -name marker-* \\)' -print0"
        )

        result = self._run_adb_command(find_cmd, capture_output=True)
        if result.returncode == 0 and result.stdout.strip():
            files = result.stdout.strip().split("\0")
            for file_path in files:
                if file_path.strip():
                    filename = os.path.basename(file_path)
                    local_path = os.path.join(self.temp_dir or "", filename)
                    self._run_adb_command(f'pull "{file_path}" "{local_path}"')

    def process_simpleperf_data(self) -> None:
        """Process simpleperf data using samply."""
        # Build command with optional symbol arguments
        cmd_parts = [f'"{self.samply_binary}"', "import", "su-perf.data"]

        # Add symbol directories (can be multiple)
        for symbol_dir in self.symbol_dirs:
            cmd_parts.extend(["--symbol-dir", f'"{symbol_dir}"'])

        # Add breakpad symbol servers (can be multiple)
        for server in self.breakpad_symbol_servers:
            cmd_parts.extend(["--breakpad-symbol-server", server])

        # Add breakpad symbol directories (can be multiple)
        for symbol_dir in self.breakpad_symbol_dirs:
            cmd_parts.extend(["--breakpad-symbol-dir", f'"{symbol_dir}"'])

        # Add final arguments
        cmd_parts.extend(["--presymbolicate"])
        cmd_parts.extend(["--save-only", "-o", self.simpleperf_profile_path])
        # cmd_parts.extend(["--per-cpu-threads"])
        # cmd_parts.extend(["--cswitch-markers"])

        cmd = " ".join(cmd_parts)
        result = self._run_command(cmd, cwd=self.temp_dir)
        if result.returncode != 0:
            raise RuntimeError("Failed to run samply import")

    def merge_profiles(self) -> None:
        """Merge the simpleperf and Gecko profiles."""
        package_name = self.config["package_name"]

        cmd = (
            f'node "{self.merge_script}" '
            f'--samples-file  "{self.simpleperf_profile_path}" '
            f'--markers-file "{self.gecko_profile_path}" '
            f'--output-file "{self.output_path}" '
            f"--filter-by-process-prefix {package_name}"
        )

        result = self._run_command(cmd, cwd=self.temp_dir)
        if result.returncode != 0:
            logger.error("Failed to merge profiles")
            self.output_path = self.simpleperf_profile_path
            # raise RuntimeError("Failed to merge profiles")

    def handle_output(self) -> None:
        """Handle the merged profile output - either save or auto-load."""
        if self.should_open_with_samply:
            # No output path specified, auto-load with samply
            logger.info("Auto-loading profile with samply...")
            self._run_samply_load(self.output_path)
        else:
            merged_profile_path = self.output_path
            # Output path was specified, profile is already saved there
            logger.info(f"Merged profile saved to: {merged_profile_path}")
            logger.info(f'To view the profile, run: samply load "{merged_profile_path}"')

    def _run_samply_load(self, profile_path: str) -> None:
        """Run samply load command with proper interactive handling."""
        cmd = f'"{self.samply_binary}" load "{profile_path}"'
        logger.debug(f"Running interactive: {cmd}")

        try:
            # Always show output for samply load (it's interactive)
            env = os.environ.copy()
            env["PROFILER_URL"] = "https://deploy-preview-5190--perf-html.netlify.app/"
            subprocess.run(cmd, shell=True, env=env)
        except KeyboardInterrupt:
            # Ctrl+C is expected for closing samply - don't treat as error
            logger.info("Profile viewer closed")
        except Exception as e:
            logger.error(f"Failed to run samply load: {e}")

    def _kill_processes(self, package_names: list[str]) -> None:
        for package_name in package_names:
            self._run_adb_command(f"shell am force-stop {package_name}")

    def kill_relevant_processes(self) -> None:
        package_name = self.config["package_name"]
        packages_to_kill = [package_name, f"{package_name}_zygote"]
        for pkg in BROWSER_PACKAGE_NAMES:
            packages_to_kill.extend([pkg, f"{pkg}_zygote"])
        self._kill_processes(packages_to_kill)
        time.sleep(2)

    def run(self) -> None:
        """Run the complete automation workflow."""
        try:
            logger.info("Starting Android profiling automation...")

            # Validate environment first
            self.validate_environment()

            if self.with_warmup:
                self.kill_relevant_processes()
                self.run_warmup_scenario()

            # Setup
            self.setup_temp_directory()
            self.setup_gecko_profiling()

            # Kill any running processes
            self.kill_relevant_processes()

            # Start profiling
            simpleperf_proc = self.start_simpleperf_recording()

            # Trigger app and capture Gecko profile
            self.trigger_app_startup()
            logger.info("Capturing Gecko profile...")
            self.capture_gecko_profile()

            # Wait for simpleperf and collect data
            self.collect_simpleperf_data(simpleperf_proc)

            # Process and merge profiles
            logger.info("Processing simpleperf data...")
            self.process_simpleperf_data()
            logger.info("Merging profiles...")
            self.merge_profiles()

            logger.info("Profiling automation completed successfully!")

            # Handle output (save or auto-load) - this may run samply interactively
            self.handle_output()

        except KeyboardInterrupt:
            logger.info("Operation cancelled by user")
        except Exception as e:
            logger.error(f"Error during automation: {e}")
            sys.exit(1)
        finally:
            self.cleanup_device_state()
            self.cleanup_temp_directory()


def main() -> None:
    parser = argparse.ArgumentParser(description="Automate Android startup profiling workflow")
    parser.add_argument(
        "--config", default="config.toml", help="Path to configuration file (default: config.toml)"
    )
    parser.add_argument(
        "--device", help="Specify Android device ID (required when multiple devices are connected)"
    )
    parser.add_argument(
        "--java",
        action="store_true",
        help="Use DWARF unwinding (-g, gives you Java stacks) instead of framepointer unwinding (--call-graph fp, allows deeper stacks and JS JIT stacks)",
    )
    parser.add_argument(
        "--with-warmup",
        action="store_true",
        help="Clear app data and run warmup before the profiling run",
    )
    parser.add_argument(
        "--profile-warmup",
        action="store_true",
        help="Profile the applink startup warmup run",
    )

    # Config overrides
    parser.add_argument("--package", help="Override package name from config")
    parser.add_argument("--url", help="Override startup URL from config")
    parser.add_argument(
        "--duration", type=int, help="Override profiling duration in seconds from config"
    )
    parser.add_argument(
        "--frequency", type=int, help="Override profiling frequency in Hz from config"
    )

    # Output options
    parser.add_argument(
        "--out",
        "-o",
        help="Output path for merged profile (if not specified, auto-loads with samply)",
    )

    # Logging options
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set the logging level (default: INFO)",
    )

    args = parser.parse_args()

    # Setup logging before creating automation instance
    setup_logging(args.log_level)

    automation = AndroidProfileAutomation(
        config_path=args.config,
        device_id=args.device,
        use_java=args.java,
        with_warmup=args.with_warmup,
        profile_warmup=args.profile_warmup,
        package=args.package,
        url=args.url,
        duration=args.duration,
        frequency=args.frequency,
        output_path=args.out,
    )
    automation.run()


if __name__ == "__main__":
    main()
