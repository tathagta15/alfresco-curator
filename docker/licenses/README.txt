Drop the Alfresco licence file (*.lic) into this directory.

It is mounted read-only into the repository container at
/usr/local/tomcat/shared/classes/alfresco/extension/license, which is where
ACS looks on startup. Restart the repository to apply it:

    docker compose -f docker/docker-compose.yml restart alfresco

Without a licence here the repository runs on a 2-day trial. When that trial
expires the repository drops to read-only and every upload fails with:

    09020059 Access Denied. The system is currently in read-only mode.

Licence files are ignored by git - see .gitignore.
