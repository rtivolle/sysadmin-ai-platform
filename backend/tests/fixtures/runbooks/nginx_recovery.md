# Runbook: Procédure de Récupération Serveur Web Nginx

## 1. Description du Service
Ce runbook couvre le diagnostic et la reprise sur incident du serveur mandataire inverse Nginx de production.

## 2. Diagnostic Rapide
Vérifier l'état du service et inspecter les dernières erreurs :
```bash
systemctl status nginx
grep -i error /var/log/nginx/error.log
```

## 3. Validation de Configuration
Ne jamais recharger Nginx sans tester la syntaxe :
```bash
nginx -t
```

## 4. Procédure de Redémarrage (Action Modificatrice)
Après validation de la configuration et approbation :
```bash
systemctl restart nginx
```

## 5. Escalade Incident P1
Si l'erreur persiste au-delà de 10 minutes ou impacte le trafic client, déclencher l'astreinte avec la clé prioritaire P1.
