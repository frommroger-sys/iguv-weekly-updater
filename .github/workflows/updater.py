- def fetch_so-fit():
+ def fetch_so_fit():
     return fetch_ao_generic("https://www.so-fit.ch",
         ["gebühr","gebuehr","tarif","reglement","prüf","pruef","faq"])

-     aos_full = { "AOOS": fetch_aoos(), "OSFIN": fetch_osfin(),
-                  "OAD FCT":fetch_oadfct(), "OSIF": fetch_osif(),
-                  "So-Fit": fetch_so_fit() }
+     aos_full = { "AOOS": fetch_aoos(), "OSFIN": fetch_osfin(),
+                  "OAD FCT": fetch_oadfct(), "OSIF": fetch_osif(),
+                  "So-Fit": fetch_so_fit() }
