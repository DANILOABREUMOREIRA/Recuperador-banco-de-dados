import json, os, shutil, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inst, run
out, db, reps = sys.argv[1], sys.argv[2], int(sys.argv[3])
DEF = ["--max_allowed_packet=64M"]
TUNED = DEF + ["--innodb_log_file_size=268435456", "--innodb_doublewrite=0", "--innodb_flush_log_at_trx_commit=2", "--innodb_buffer_pool_size=536870912", "--innodb_log_buffer_size=16777216"]
SC = {

    "insert_default": (DEF, [("dump.sql", [])]),
    "load_default": (DEF, [("estrutura.sql", []), ("carregar.sql", ["--local-infile=1"])]),
    "load_default_idx": (DEF, [("estrutura2.sql", []), ("carregar.sql", ["--local-infile=1"]), ("indices.sql", [])]),
    "insert_tuned": (TUNED, [("dump.sql", [])]),
    "load_tuned_idx": (TUNED, [("estrutura2.sql", []), ("carregar.sql", ["--local-infile=1"]), ("indices.sql", [])]),
}
log = open(os.path.join(os.environ["TEMP"], "lt", f"bench_{db}.jsonl"), "a")
n = 0
for r in range(reps):
    for name, (flags, steps) in SC.items():
        n += 1; port = 3400 + n % 50
        inst.start(f"bn{n}", port, flags)
        t = sum(run.imp(port, f, out, x) for f, x in steps)
        inst.stop(port); time.sleep(3)
        shutil.rmtree(os.path.join(inst.ROOT, f"bn{n}"), ignore_errors=True)
        log.write(json.dumps({"db": db, "rep": r, "scenario": name, "seconds": round(t, 1)}) + "\n"); log.flush()
