from __future__ import annotations

import copy

from .runtime import digest, normalized


def graph_plan(extracted, chunk, evidence, snapshot, context_nodes, semantic_matches=None):
    """复用语义匹配节点，归并片段子图，不调用模型。"""
    semantic_matches = semantic_matches or {}
    canonical_ids = {old: nid for nid, node in snapshot['nodes'].items() for old in node.get('merged_from', [])}
    allowed, node_map, changes = {}, {}, []
    request_id = chunk['id'] + '_merge_v' + str(snapshot['version'])
    group = 'fragment'
    reserved = {row['claim_id'] for row in extracted.get('revisions', [])}

    def add(operation, target, payload, ids, reason, compared=()):
        changes.append(dict(proposal_id='p' + str(len(changes) + 1), group_id=group,
                            operation=operation, target_id=target, payload=payload,
                            evidence_ids=list(dict.fromkeys(ids)), compared_evidence_ids=list(compared), reason=reason))

    def include(nid):
        if nid not in allowed:
            allowed[nid] = dict(copy.deepcopy(snapshot['nodes'][nid]), lifecycle='existing', identity_status='resolved')

    for concept in extracted['concepts']:
        key = (normalized(concept['name']), concept['entity_type'], normalized(concept.get('scope', '')))
        matched = semantic_matches.get(concept['query_id'])
        nid = node_map.get(matched, matched) if matched else None
        if nid is None:
            nid = 'temp_' + digest([chunk['id'], key, concept['query_id']])[:24]
            allowed[nid] = dict(copy.deepcopy(concept), id=nid, node_kind='concept',
                                lifecycle='provisional', identity_status='resolved')
            add('add_node', nid, {}, concept['evidence_ids'], '引入教材明确描述的实体。')
        elif nid in snapshot['nodes']:
            include(nid)
        node_map[concept['query_id']] = nid
        allowed[nid]['evidence_ids'] = list(dict.fromkeys(allowed[nid].get('evidence_ids', []) + concept['evidence_ids']))

    facts = {}
    definitions = {}
    for claim in snapshot['claims'].values():
        content = claim['content']
        if claim['status'] == 'active' and content['kind'] == 'node_fact':
            key = (content['node_id'], content['field'], content['value'], content.get('conditions', ''))
            facts[key] = claim
            if content['field'] == 'definition' and not content.get('conditions'):
                definitions[content['node_id']] = claim
    for nid, node in allowed.items():
        if node['lifecycle'] == 'provisional' and node.get('definition') and node['definition_status'] != 'partial':
            definitions[nid] = {'content': {'kind': 'node_fact', 'node_id': nid, 'field': 'definition',
                                          'value': node['definition'], 'definition_status': node['definition_status'], 'conditions': ''}}

    def fact(nid, field, value, ids, status=None, conditions=''):
        content = dict(kind='node_fact', node_id=nid, field=field, value=value, conditions=conditions)
        if status:
            content['definition_status'] = status
        existing = facts.get((nid, field, value, conditions))
        if existing:
            if existing['id'] not in reserved:
                # 相同字段复用旧断言的表述，避免仅状态标签不同而重复建事实。
                add('attach_evidence', existing['id'], {}, ids, '累积当前教材对既有事实的证据。')
        else:
            add('add_claim', None, {'claim': content}, ids, '保存当前教材的实体属性。')
        return content

    for concept in extracted['concepts']:
        nid = node_map[concept['query_id']]
        ids = concept['evidence_ids']
        fact(nid, 'entity_type', allowed[nid]['entity_type'], ids)
        if allowed[nid].get('scope'):
            fact(nid, 'scope', allowed[nid]['scope'], ids)
        if concept.get('scope') and concept['scope'] != allowed[nid].get('scope', ''):
            fact(nid, 'scope', concept['scope'], ids, conditions='教材语境：' + concept['scope'])
        aliases = concept.get('aliases', []) + ([concept['name']] if concept['name'] != allowed[nid]['name'] else [])
        for alias in aliases:
            fact(nid, 'alias', alias, ids)
        if concept.get('definition'):
            previous = definitions.get(nid)
            definition_ids = concept['definition_evidence_ids']
            status = concept['definition_status']
            value = concept['definition']
            if previous and previous.get('id') in reserved:
                continue
            if status != 'partial' and previous and previous['content']['value'] == value:
                fact(nid, 'definition', value, definition_ids, previous['content']['definition_status'])
            elif status != 'partial' and previous and previous.get('origin') == 'parametric_prior' and not previous['evidence_ids']:
                after = dict(previous['content'], value=value, definition_status=status)
                add('revise_claim', previous['id'], {'before': previous['content'], 'after': after},
                    definition_ids, '用教材定义更新尚无教材证据的先验定义。')
                definitions[nid] = {'content': after}
            elif status != 'partial' and not previous:
                content = fact(nid, 'definition', value, definition_ids, status)
                definitions[nid] = {'content': content}
            else:
                fact(nid, 'definition_detail', value, definition_ids, status)

    context_ids = {node['id'] for node in context_nodes}

    def endpoint(value):
        if value in node_map:
            return node_map[value]
        if value not in context_ids:
            raise ValueError('Relation endpoint was not supplied to extraction: ' + str(value))
        value = canonical_ids.get(value, value)
        include(value)
        return value

    omitted_relations = []
    for relation in extracted['relations']:
        content = dict(kind='relation', head=endpoint(relation['head']), tail=endpoint(relation['tail']),
                       relation=relation['relation'], conditions=relation.get('conditions', ''))
        if content['head'] == content['tail']:
            continue
        if content['relation'] == 'evaluated_by' and allowed[content['tail']]['entity_type'] != 'Metric':
            omitted_relations.append(dict(relation=relation, reason='evaluated_by 的终点必须是 Metric。'))
            continue
        add('add_claim', None, {'claim': content}, relation['evidence_ids'], '保存教材明确支持的关系及条件。')

    reference_ids, deferred_revisions = set(), []
    for revision in extracted.get('revisions', []):
        old = snapshot['claims'][revision['claim_id']]
        # 已被其他已提交片段改变的旧断言不再覆盖，当前抽取事实仍正常累积。
        if (old['status'] != 'active' or old['content'] != revision['before'] or
                old['revision'] != revision['observed_revision']):
            deferred_revisions.append(dict(claim_id=old['id'], reason='原断言已在其他片段提交时更新。', proposal=revision))
            continue
        before = old['content']
        for key in ('node_id',) if before['kind'] == 'node_fact' else ('head', 'tail'):
            include(before[key])
        compared = old.get('evidence_ids', [])
        reference_ids.update(compared)
        if revision['operation'] == 'retire_claim':
            add('retire_claim', old['id'], {}, revision['evidence_ids'], revision['reason'], compared)
        else:
            after = copy.deepcopy(revision['after'])
            for key in ('node_id',) if after['kind'] == 'node_fact' else ('head', 'tail'):
                after[key] = endpoint(after[key])
            add('revise_claim', old['id'], {'before': before, 'after': after}, revision['evidence_ids'], revision['reason'], compared)

    # 同一片段可能多次提及同一事实，提交前合并对应的证据。
    unique = {}
    for change in changes:
        key = digest([change['operation'], change['target_id'], change['payload']])
        if key in unique:
            kept = unique[key]
            kept['evidence_ids'] = list(dict.fromkeys(kept['evidence_ids'] + change['evidence_ids']))
        else:
            unique[key] = change
    proposal = dict(request_id=request_id, base_graph_version=snapshot['version'],
                    changes=list(unique.values()), follow_up=[])
    return dict(chunk=chunk, proposal=proposal, allowed=list(allowed.values()), evidence=evidence,
                reference_ids=sorted(reference_ids), extracted=extracted, context_nodes=context_nodes,
                snapshot_version=snapshot['version'], deferred_revisions=deferred_revisions,
                omitted_relations=omitted_relations)
