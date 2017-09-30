import os


def create_file_data(file_data, repo_workdir, prefix=""):
    paths = []
    if file_data is None:
        file_data = {"README": "Example change\n"}
    for rel_path, contents in file_data.iteritems():
        repo_path = os.path.join(prefix, rel_path)
        path = os.path.join(repo_workdir, repo_path)
        paths.append(repo_path)
        with open(path, "w") as f:
            f.write(contents)
    return paths
