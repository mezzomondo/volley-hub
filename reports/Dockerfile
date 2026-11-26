# Report worker: R + datavolley + volleyreport + pagedown (headless Chrome) running the coach's report scripts.
FROM rocker/r-ver:4.4.2
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 libxml2 libfontconfig1 libfreetype6 libharfbuzz0b libfribidi0 libpng16-16 libjpeg-turbo8 libtiff6 \
      fonts-dejavu-core fonts-liberation pandoc wget ca-certificates \
 && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb && rm /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*
RUN install2.r --error --skipinstalled DBI RPostgres dplyr tidyr ggplot2 scales kableExtra htmltools knitr \
      pagedown jsonlite \
 && Rscript -e 'install.packages(c("datavolley", "volleyreport"), repos = c("https://openvolley.r-universe.dev", getOption("repos")))' \
 && Rscript -e 'for (p in c("datavolley", "volleyreport", "pagedown", "kableExtra")) stopifnot(requireNamespace(p))' \
 && rm -rf /tmp/downloaded_packages
# Chrome refuses to run as root without --no-sandbox, and the scripts call pagedown::chrome_print() without
# extra arguments: pagedown uses the browser in $PAGEDOWN_CHROME, a wrapper that adds the flags.
RUN printf '#!/bin/sh\nexec /usr/bin/google-chrome --no-sandbox --disable-gpu --disable-dev-shm-usage "$@"\n' \
      > /usr/local/bin/chrome-headless && chmod +x /usr/local/bin/chrome-headless
ENV PAGEDOWN_CHROME=/usr/local/bin/chrome-headless
WORKDIR /reports
COPY worker.R ./
CMD ["Rscript", "worker.R"]
