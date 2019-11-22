FROM ubuntu:bionic
ENV LANG C.UTF-8
USER root
# Install base files
RUN set -x ; \
    apt-get update \
    && apt-get install -y --no-install-recommends \
    apt-transport-https \
    build-essential \
    ca-certificates \
    curl \
    fonts-freefont-ttf \
    fonts-noto-cjk \
    gawk \
    gnupg \
    libldap2-dev \
    libsasl2-dev \
    libxslt1-dev \
    node-less \
    python \
    python-dev \
    python-pip \
    python3 \
    python3-dev \
    python3-pip \
    python3-setuptools \
    python3-wheel \
    sed \
    sudo \
    unzip \
    xfonts-75dpi \
    zip \
    zlib1g-dev

# Install Google Chrome
RUN curl -sSL http://nightly.odoo.com/odoo.key | apt-key add - \
    && echo "deb http://nightly.odoo.com/deb/bionic ./" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \ 
    && apt-get install -y -qq google-chrome-stable

# Install phantomjs
RUN curl -sSL https://bitbucket.org/ariya/phantomjs/downloads/phantomjs-2.1.1-linux-x86_64.tar.bz2  -o /tmp/phantomjs.tar.bz2 \
    && tar xvfO /tmp/phantomjs.tar.bz2 phantomjs-2.1.1-linux-x86_64/bin/phantomjs > /usr/local/bin/phantomjs \
    && chmod +x /usr/local/bin/phantomjs \
    && rm -f /tmp/phantomjs.tar.bz2

# Install wkhtml
RUN curl -sSL https://github.com/wkhtmltopdf/wkhtmltopdf/releases/download/0.12.5/wkhtmltox_0.12.5-1.bionic_amd64.deb -o /tmp/wkhtml.deb \
    && apt-get update \
    && dpkg --force-depends -i /tmp/wkhtml.deb \
    && apt-get install -y -f --no-install-recommends \
    && rm /tmp/wkhtml.deb

# Install rtlcss (on Debian stretch)
RUN curl -sSL https://deb.nodesource.com/gpgkey/nodesource.gpg.key | apt-key add - \
    && echo "deb https://deb.nodesource.com/node_8.x stretch main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y nodejs

RUN npm install -g rtlcss

# Install es-check tool
RUN npm install -g es-check

# Install for migration scripts
RUN apt-get update \
    && apt-get install -y python3-markdown

# Install flamegraph.pl
ADD https://raw.githubusercontent.com/brendangregg/FlameGraph/master/flamegraph.pl /usr/local/bin/flamegraph.pl
RUN chmod +rx /usr/local/bin/flamegraph.pl

# Install Odoo Debian dependencies
ADD https://raw.githubusercontent.com/odoo/odoo/10.0/debian/control /tmp/p2-control
ADD https://raw.githubusercontent.com/odoo/odoo/master/debian/control /tmp/p3-control
RUN pip install -U setuptools wheel \
    && apt-get update \
    && sed -n '/^Depends:/,/^[A-Z]/p' /tmp/p2-control /tmp/p3-control | awk '/^ [a-z]/ { gsub(/,/,"") ; print }' | sort -u | sed 's/python-imaging/python-pil/'| sed 's/python-pypdf/python-pypdf2/' | DEBIAN_FRONTEND=noninteractive xargs apt-get install -y -qq \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Odoo requirements for python2 and python3 not fullfilled by Debian dependencies
ADD https://raw.githubusercontent.com/odoo/odoo/master/requirements.txt /root/p3-requirements.txt
ADD https://raw.githubusercontent.com/odoo/odoo/10.0/requirements.txt /root/p2-requirements.txt
RUN pip install --no-cache-dir -r /root/p2-requirements.txt coverage flanker==0.4.38 pylint==1.7.2 phonenumbers redis \
    && pip3 install --no-cache-dir -r /root/p3-requirements.txt coverage websocket-client astroid==2.0.4 pylint==1.7.2 phonenumbers pyCrypto dbfread==2.0.7 firebase-admin==2.17.0 flamegraph
