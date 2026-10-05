from task_domain_006.core import Record, stable_summary

if __name__ == "__main__":
    print(stable_summary(Record("demo", "v1", "draft", "operator")))
