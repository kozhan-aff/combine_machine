"""Google Search Console client. Transport only.

Auth: service account (settings.GSC_SERVICE_ACCOUNT_JSON). Each domain is a GSC property.
- Verify ownership: add TXT record via Cloudflare (see cloudflare.add_txt_record).
- Submit sitemap: PUT https://www.googleapis.com/webmasters/v3/sites/{siteUrl}/sitemaps/{feedpath}
- Index status:   POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect
                  -> write Page.index_status + IndexHistory row. Mind daily inspection quota.
Use google-api-python-client + google-auth.
"""


class GscClient:
    def __init__(self, service_account_json: str):
        self.service_account_json = service_account_json

    def verify_property(self, site_url: str) -> dict:
        raise NotImplementedError

    def submit_sitemap(self, site_url: str, feedpath: str) -> dict:
        raise NotImplementedError

    def inspect_url(self, site_url: str, page_url: str) -> dict:
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError
