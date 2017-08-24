# Use an official Python runtime as a parent image
FROM ubuntu

RUN apt-get update && \
    apt-get install -y libcurl3 git python python-pip python-requests && \
    git config --global user.name wpt-sync && \
    git config --global user.email wpt-sync@lists.mozilla.com


WORKDIR /git-cinnabar

RUN git clone https://github.com/glandium/git-cinnabar.git . && git checkout 0.4.0

ENV PATH=/git-cinnabar:$PATH

RUN git cinnabar download

# Set the working directory to /app
WORKDIR /wpt-sync

# Copy the current directory contents into the container at /app
ADD . /wpt-sync

# Install any needed packages specified in requirements.txt
RUN pip install -r requirements.txt
