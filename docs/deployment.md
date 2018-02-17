# Deployment

Deploying the wptsync service consists of two major steps:

*   provision the server with our Docker image and dependencies: this uses an 
    ansible playbook; the docker-build step may be skipped,
*   ssh into the server to start the service, optionally seeding gecko and
    web-platform-test repositories first.

Terms:

*   control machine: this is where you're running the ansible playbook
*   server: where you're deploying to

## Prerequisites

Control machine:

*   Docker is installed.
*   You have access to a server configured in `ansible/hosts`.
    the ansible_ssh_user has passwordless sudo privileges.
*   Local ssh config specifies the correct username and key file with
    which to connect to the server.
*   You have set extra_vars and environment variables as described in 
    `./bin/provision.sh`.

Server:

*   There is a user designated to run the wptsync service: `wpt_user`.
*   There are paths as described in `ansible/hosts`; these should be
    writable by `wpt_user`.
*   (optional) There is a bare git-cinnabar clone of mozilla-unified whose
    config matches `docker/gecko_config`. For example, you can scp an
    archive of a recent clone to the server, and extract it at the path that will be mounted to `/app/repos/gecko` in the Docker container. 
    This significantly speeds up repo seeding (`git fetch mozilla; git fetch autoland`) when you first start the wptsync service.


## Provisioning steps

The provisioning steps are configured in ansible playbooks at 
`ansible/wptsync_deploy.yml`, `ansible/wptsync_update.yml` and the role at 
`ansible/roles/wptsync_host`. The playbooks have been tested against a minimal 
Centos 7 host.

1.  Activate a virtualenv and `pip install -r ./requirements/deploy.txt`
2.  Do __one__ of the following from the repo root:
    *   If you need to build and push a new Docker image or update credentials 
        run `./bin/provision.sh`
    *   If you only need to update the wpt-sync repo to the latest commit on
        master, run `./bin/update_server.sh`

As a result, the following will be up-to-date: the docker image, the data it
depends on as well as `run_docker.sh`, which is generated from `ansible/roles/
wptsync_host/templates/run_docker.sh.j2` and runnable by `wpt_user`. Note 
where this script is installed.

## Starting/stopping the wptsync service

The ansible playbooks stop any running containers, so we need to restart them.

1.  ssh into the server. If necessary, also `sudo su wpt_user`.
2.  Optionally, `run_docker.sh run --shell` and seed the repos:

    ```
    wptsync repo-config web-platform-tests /app/wpt-sync/docker/wpt_config
    wptsync fetch web-platform-tests
    wptsync repo-config gecko /app/wpt-sync/docker/gecko_config
    wptsync fetch gecko
    cd /app/repos/gecko
    git fetch autoland
    ```

3.  To start the service: 

    ```
    screen -dmS wptsync run_docker.sh run
    ```

    Or to start the service with a particular docker image:

    ```
    screen -dmS wptsync run_docker.sh run --image <imagename>:<tag>
    ```

    You can see what images are available with `docker images`.

4. To stop the service use `docker stop -t 30 <container_name>`. `docker ps` will tell you the container names.

See the [user guide](./user-guide.md) for troubleshooting instructions. 
