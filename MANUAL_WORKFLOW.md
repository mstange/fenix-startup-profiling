# How to create merged profiles from simpleperf and the Gecko Profiler

For org.mozilla.fenix:

Put the following into org.mozilla.fenix-geckoview-config.yaml

```
env:
  PERF_SPEW_DIR: /storage/emulated/0/Android/data/org.mozilla.fenix/files
  IONPERF: func
  JIT_OPTION_emitInterpreterEntryTrampoline: true
  JIT_OPTION_enableICFramePointers: true
  JIT_OPTION_onlyInlineSelfHosted: true

  MOZ_PROFILER_STARTUP: 1
  MOZ_PROFILER_STARTUP_NO_BASE=: 1 # bug 1955125
  MOZ_PROFILER_STARTUP_INTERVAL: 500
  MOZ_PROFILER_STARTUP_FEATURES: nostacksampling,nomarkerstacks,screenshots,ipcmessages,java,cpu,markersallthreads
  MOZ_PROFILER_STARTUP_FILTERS: GeckoMain,Compositor,Renderer,IPDL Background,*
```

Run the following in one terminal window:

```
adb push org.mozilla.fenix-geckoview-config.yaml /data/local/tmp/
adb shell am set-debug-app --persistent org.mozilla.fenix
adb shell su -c "/data/local/tmp/simpleperf record --call-graph fp --duration 10 -f 1000 --trace-offcpu -e cpu-clock -a -o /data/local/tmp/su-perf.data"
```

Then trigger the app link startup sequence in a different terminal window:

```
adb shell am force-stop org.mozilla.fenix && adb shell am start-activity -d "https://shell--mozilla-speedometer-preview.netlify.app/resources/newssite/news-nuxt/dist/index.html" -a android.intent.action.VIEW org.mozilla.fenix/org.mozilla.fenix.IntentReceiverActivity
```

When startup is complete, capture the Gecko Profile:

```
adb shell am start-service -n org.mozilla.fenix.debug/org.mozilla.fenix.perf.ProfilerService -a mozilla.perf.action.STOP_PROFILING --es "output_filename" my-startup-profile
# sleep for two seconds
adb pull /storage/emulated/0/Download/my-startup-profile.json my-startup-profile.json.gz
```

Once simpleperf in the first terminal window has stopped profiling (after 10 seconds), run the following:

```
adb pull /data/local/tmp/su-perf.data
adb shell find /storage/emulated/0/Android/data/org.mozilla.fenix/files '\( -name  jit-* -or -name marker-* \)' -print0 | xargs -0 -I {} adb pull '{}'
adb shell am clear-debug-app
~/code/samply/target/release/samply import su-perf.data --symbol-dir ~/code/obj-m-android/dist/bi --breakpad-symbol-server https://symbols.mozilla.org/ --breakpad-symbol-dir /Users/mstange/Downloads/target.crashreporter-symbols\(5\) --presymbolicate --save-only -o simpleperf.json.gz
```

Then merge the two profiles into one merged-profile.json.gz:

```
node ~/code/fenix-startup-profiling/merge-android-profiles/merge-android-profiles.js --samples-file simpleperf.json.gz --markers-file my-startup-profile.json.gz --output-file merged-profile.json.gz --filter-by-process-prefix org.mozilla.fenix
```

Then open the merged profile using `samply load merged-profile.json.gz`.