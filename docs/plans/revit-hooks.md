# Hooks Revit pilotés par l'IA : viabilité et plan

## Context

L'IA exécute du code IronPython dans Revit via la route `/execute_code/` ([revit_mcp/code_execution.py](revit_mcp/code_execution.py)).
Le code tourne sur le thread UI de Revit (pyRevit Routes le marshale via ExternalEvent, la requête HTTP attend la fin).
Si une fenêtre modale apparaît (TaskDialog, avertissement de transaction, MessageBox), le thread UI se bloque.
Le client MCP expire alors après 60 s ([tools/code_execution_tools.py:50](tools/code_execution_tools.py#L50)) sans savoir pourquoi, et Revit reste figé jusqu'à ce qu'un humain clique.
Aucun mécanisme de gestion de dialogues ou d'échecs n'existe aujourd'hui dans le dépôt.

Objectif : permettre à l'IA de déclarer à l'avance comment répondre aux dialogues et avertissements, de recevoir un rapport de ce qui est apparu, et que tout soit désactivé hors des exécutions IA pour laisser l'utilisateur travailler normalement.

## Verdict de viabilité

**Viable, avec une contrainte de conception majeure : les réponses doivent être déclaratives et préparées avant l'exécution.**

- `DialogBoxShowing` est déclenché de façon synchrone sur le thread UI, pendant que la requête HTTP de l'IA est encore en attente. L'IA ne peut donc pas « répondre en direct » à un dialogue. Le gestionnaire doit décider seul, immédiatement, à partir de règles fournies d'avance.
- Le modèle viable est une boucle : règles déclarées → exécution → rapport des dialogues rencontrés (gérés ou non) → l'IA ajuste ses règles et relance. Ça couvre l'autonomie visée sans problème d'async.
- Un dialogue non couvert par une règle peut être fermé avec une réponse « sûre » par défaut (Cancel/Close) plutôt que de bloquer. C'est le principal gain anti-blocage.
- Limites : `DialogBoxShowing` ne voit que les dialogues gérés par Revit. Les fenêtres WPF/WinForms d'add-ins tiers et certaines boîtes Win32 natives lui échappent. On ne peut pas non plus interrompre du code IronPython qui boucle sur le thread UI.
- Les avertissements de transaction se traitent mieux par `IFailuresPreprocessor` (implémentable en IronPython en héritant de `DB.IFailuresPreprocessor`) ou globalement via l'événement `Application.FailuresProcessing`.
- Désactivation : on s'abonne **une seule fois** au démarrage et le gestionnaire consulte un drapeau « armé ». Hors exécution IA, il retourne immédiatement et l'utilisateur voit ses dialogues normalement. C'est plus sûr que s'abonner et se désabonner à chaque appel.

**Risques à maîtriser**
- Une exception non attrapée dans un gestionnaire d'événement peut déstabiliser Revit. Tout gestionnaire doit être enveloppé dans try/except.
- Le rechargement de pyRevit réexécute [startup.py](startup.py) : sans garde, les gestionnaires s'empilent. Il faut un singleton stocké hors du module, par exemple via `System.AppDomain.CurrentDomain.SetData`, et se désabonner de l'ancien délégué avant de réabonner.
- Supprimer des avertissements à l'aveugle peut masquer des problèmes de modèle. Par défaut, on collecte et on rapporte, on ne supprime que sur règle explicite.
- Laisser l'IA injecter du code arbitraire comme gestionnaire est possible mais fragile. On commence par des règles déclaratives seulement.

## Approche recommandée

### 0. Versionner ce plan dans le repo

- Copier ce document tel quel dans `docs/plans/revit-hooks.md` du dépôt, avant toute implémentation.
- Ne rien implémenter d'autre tant que l'utilisateur n'a pas relu ce fichier dans le repo.

### 1. Nouveau module `revit_mcp/hooks.py` (côté Revit, IronPython)

- Classe `HookManager` singleton, récupérée/stockée via `AppDomain.CurrentDomain.GetData/SetData("revit_mcp.hooks")`.
- `install(uiapp)` : se désabonne de l'éventuel ancien délégué, puis s'abonne à `uiapp.DialogBoxShowing` et `uiapp.Application.FailuresProcessing`.
- État : `armed` (bool), `rules` (liste), `events_log` (liste), `default_dialog_action` (`"cancel"` | `"none"`).
- Règle de dialogue : `{"match": {"dialog_id": ..., "message_regex": ...}, "result": <int ou nom de TaskDialogResult/DialogResult>, "scope": "execution" | "session", "ttl_seconds": ...}`.
- Règle d'échec : `{"match": {"severity": "warning"|"error", "description_regex": ..., "failure_id_guid": ...}, "action": "delete_warning" | "resolve" | "rollback" | "collect"}`.
- `_on_dialog(sender, e)` : try/except total. Si non armé, `return`. Sinon journalise type d'args, `DialogId`, `Message`, boutons, applique la première règle qui correspond via `e.OverrideResult(...)`, sinon applique l'action par défaut.
- `_on_failures(sender, e)` : via `e.GetFailuresAccessor()`, journalise chaque message (sévérité, description, éléments), applique les règles, `e.SetProcessingResult(...)`.
- `arm(rules)` / `disarm()` : le désarmement purge les règles de portée `execution` et retourne le journal.

### 2. Brancher sur `/execute_code/` ([revit_mcp/code_execution.py](revit_mcp/code_execution.py))

- Accepter dans le payload `dialog_rules`, `failure_rules`, `default_dialog_action` (défaut `"cancel"`).
- `hook_manager.arm(...)` avant `exec`, `disarm()` dans un `finally` pour garantir le retour au mode utilisateur.
- Ajouter au résultat succès et erreur un champ `revit_events` : dialogues vus, réponse appliquée, règle correspondante, avertissements collectés ou supprimés.
- Exposer `hooks` dans le `namespace` d'exécution pour que l'IA puisse, si besoin, utiliser un helper `hooks.transaction(doc, "nom")` qui pose un `IFailuresPreprocessor` sur la transaction.

### 3. Routes de gestion (dans `hooks.py`, enregistrées depuis [startup.py](startup.py))

- `GET /hooks/` : état, règles de session, derniers événements.
- `POST /hooks/rules/` : ajouter/remplacer des règles de portée `session` avec TTL obligatoire.
- `POST /hooks/clear/` : tout désarmer et purger.
- Ces routes ne doivent pas dépendre du thread UI pour lire l'état. À vérifier : pyRevit Routes n'exécute hors ExternalEvent que les handlers sans paramètre `doc`/`uidoc`/`uiapp`.

### 4. Côté MCP (Python CPython)

- [tools/code_execution_tools.py](tools/code_execution_tools.py) : ajouter les paramètres optionnels `dialog_rules`, `failure_rules`, `default_dialog_action` à `execute_revit_code` et documenter dans la docstring la boucle « exécuter → lire `revit_events` → ajuster les règles ».
- Nouveau `tools/hook_tools.py` avec `get_revit_hooks`, `set_revit_hook_rules`, `clear_revit_hooks`, enregistré comme les autres modules dans [main.py](main.py).
- Mettre à jour [LLM.txt](LLM.txt) et le README avec des exemples de règles courantes.

### 5. Application aux autres routes

- `/open_document/`, `/close_document/`, `/sync_with_central/` ([revit_mcp/document.py](revit_mcp/document.py)) souffrent du même problème de dialogues. Dans un second temps, les envelopper avec le même arm/disarm.

## Fichiers touchés

- Nouveau : `revit_mcp/hooks.py`, `tools/hook_tools.py`, `tests/unit/test_hook_tools.py`
- Modifiés : [revit_mcp/code_execution.py](revit_mcp/code_execution.py), [startup.py](startup.py), [tools/code_execution_tools.py](tools/code_execution_tools.py), [main.py](main.py), [LLM.txt](LLM.txt)
- Réutiliser `normalize_string` de [revit_mcp/utils.py](revit_mcp/utils.py) pour les messages de dialogue accentués.

## Vérification

1. Tests unitaires côté MCP : les nouveaux paramètres sont transmis dans le payload, sur le modèle de [tests/unit/test_tool_wrappers.py](tests/unit/test_tool_wrappers.py).
2. Dans Revit, recharger pyRevit deux fois et vérifier via `GET /hooks/` qu'un seul abonnement existe.
3. Test dialogue : exécuter `from Autodesk.Revit.UI import TaskDialog; TaskDialog.Show("t", "m")` sans règle. Attendu : retour immédiat, `revit_events` montre le dialogue fermé par défaut, pas de timeout.
4. Test avertissement : créer deux murs superposés dans une transaction. Attendu : avertissement « murs qui se chevauchent » rapporté dans `revit_events`, supprimé seulement si une règle `delete_warning` est fournie.
5. Test désarmement : après l'exécution, déclencher manuellement le même dialogue dans Revit. Il doit s'afficher normalement à l'utilisateur.
6. Test robustesse : une règle avec regex invalide ne doit ni planter Revit ni bloquer l'exécution.
