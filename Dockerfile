# cb4
#
# VERSION               0.1.0

FROM ubuntu:18.04

ENV HOME="/root"

# Install dependencies
COPY ./sources.list /etc/apt/
RUN apt update && \
    apt install -y python3-dev python3-pip curl git && \
    curl -o- https://raw.githubusercontent.com/creationix/nvm/v0.33.11/install.sh | bash

# Install node.js
ENV NVM_DIR="$HOME/.nvm"
RUN \. "$NVM_DIR/nvm.sh" && nvm install 9

# Copy the cb4 files
COPY ./scripts /srv/cb4/scripts
COPY ./vj4 /srv/cb4/vj4
COPY ./.git /srv/cb4/.git
COPY ./package.json ./requirements.txt /srv/cb4/
WORKDIR /srv/cb4

# Install python dependencies and build node modules
RUN \. "$NVM_DIR/nvm.sh" && nvm use 9 && \
    npm install --registry=https://registry.npm.taobao.org && npm run build
RUN pip3 install -r ./requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple/

ENV host="localhost" \
    port=34765 \
    url-prefix="localhost" \
    oauth="" \
    oauth-client-id="" \
    oauth-client-secret="" \
    db-host="localhost" \
    db-name="cb4-production"

EXPOSE $port

CMD python3 -m vj4.server \
    --listen=http://$host:$port \
    --url-prefix=$url-prefix \
    --oauth=$oauth \
    --oauth-client-id=$oauth-client-id \
    --oauth-client-secret=$oauth-client-secret \
    --db-host=$db-host \
    --db-name=$db-name
