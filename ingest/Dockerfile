# R + datavolley for loading .dvw files on the server.
FROM rocker/r-ver:4.4.2
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/*
RUN install2.r --error --skipinstalled DBI RPostgres dplyr \
 && Rscript -e 'install.packages("datavolley", repos = c("https://openvolley.r-universe.dev", getOption("repos")))' \
 && Rscript -e 'stopifnot(requireNamespace("datavolley"))' \
 && rm -rf /tmp/downloaded_packages
WORKDIR /ingest
COPY *.R ./
ENTRYPOINT ["Rscript"]
