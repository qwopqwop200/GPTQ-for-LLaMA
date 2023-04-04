import time

import torch
import torch.nn as nn

from gptq import *
from modelutils import *
from quant import *

import os
import numpy as np
import pandas as pd

def get_t5(model):
    def skip(*args, **kwargs):
        pass
    torch.nn.init.kaiming_uniform_ = skip
    torch.nn.init.uniform_ = skip
    torch.nn.init.normal_ = skip
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer 
    model_max_length = AutoTokenizer.from_pretrained(model, use_fast=False).model_max_length
    model = AutoModelForSeq2SeqLM.from_pretrained(model, torch_dtype='auto')
    model.seqlen = model_max_length
    return model

@torch.no_grad()
def t5_sequential(model, dataloader, dev):
    print('Starting ...')

    use_cache = model.decoder.config.use_cache
    model.decoder.config.use_cache = False
    model.config.use_cache = False
    
    layers = model.encoder.block
    model.encoder.embed_tokens = model.encoder.embed_tokens.to(dev)
    layers[0] = layers[0].to(dev)

    dtype = next(iter(model.parameters())).dtype
    inps = torch.zeros((args.nsamples, model.seqlen, model.encoder.config.d_model), dtype=dtype, device=dev)
    cache = {'i': 0, 'attention_mask': None}

    class Catcher(nn.Module):
        def __init__(self, module):
            super().__init__()
            self.module = module
        def forward(self, inp, **kwargs):
            inps[cache['i']] = inp
            cache['i'] += 1
            cache['attention_mask'] = kwargs['attention_mask']
            raise ValueError
    layers[0] = Catcher(layers[0])
    for batch in dataloader:
        try:
            model(batch[0].to(dev))
        except ValueError:
            pass
    layers[0] = layers[0].module
    layers[0] = layers[0].cpu()
    
    model.encoder.embed_tokens = model.encoder.embed_tokens.cpu()
    torch.cuda.empty_cache()

    outs = torch.zeros_like(inps)
    attention_mask = cache['attention_mask']
    print('Ready.')

    quantizers = {}
    for i in range(len(layers)):
        layer = layers[i].to(dev)
        full = find_layers(layer)
        sequential = [list(full.keys())]
       
        for names in sequential:
            subset = {n: full[n] for n in names}
            gptq = {}
            for name in subset:
                gptq[name] = GPTQ(subset[name])
                gptq[name].quantizer = Quantizer()
                gptq[name].quantizer.configure(
                    args.wbits, perchannel=True, sym=args.sym, mse=False
                )
                
            def add_batch(name):
                def tmp(_, inp, out):
                    gptq[name].add_batch(inp[0].data, out.data)
                return tmp
            handles = []
            for name in subset:
                handles.append(subset[name].register_forward_hook(add_batch(name)))
            for j in range(args.nsamples):
                outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
            for h in handles:
                h.remove()

            for name in subset:
                print(f'Quantizing {name} in layer {i+1}/{len(layers)}...')
                scale,zero,g_idx = gptq[name].fasterquant(percdamp=args.percdamp, groupsize=args.groupsize, actorder=args.act_order)
                quantizers['encoder.block.%d.%s' % (i, name)] = (gptq[name].quantizer.cpu(),scale.cpu(),zero.cpu(),g_idx.cpu())
                gptq[name].free()
                
        for j in range(args.nsamples):
            outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]

        layers[i] = layer.cpu()
        del layer
        del gptq 
        torch.cuda.empty_cache()

        inps, outs = outs, inps
        
    encoder_hidden_states = torch.ones((args.nsamples, model.seqlen, model.decoder.config.d_model), dtype=dtype, device=dev)
    
    layers = model.decoder.block
    model.encoder.embed_tokens = model.encoder.embed_tokens.to(dev)
    model.decoder.embed_tokens = model.decoder.embed_tokens.to(dev)
    layers[0] = layers[0].to(dev)

    cache = {'i': 0, 'attention_mask': None}

    class Catcher(nn.Module):
        def __init__(self, module):
            super().__init__()
            self.module = module
        def forward(self, inp, **kwargs):
            inps[cache['i']] = inp
            cache['i'] += 1
            cache['attention_mask'] = kwargs['attention_mask']
            cache['encoder_attention_mask'] = kwargs['encoder_attention_mask']
            raise ValueError
    layers[0] = Catcher(layers[0])
    for batch in dataloader:
        try:
            model(decoder_input_ids = batch[0].to(dev),encoder_outputs = [encoder_hidden_states[:1],])
        except ValueError:
            pass
    layers[0] = layers[0].module
    layers[0] = layers[0].cpu()
    
    model.encoder.embed_tokens = model.encoder.embed_tokens.cpu()
    model.decoder.embed_tokens = model.decoder.embed_tokens.cpu()
    torch.cuda.empty_cache()

    dtype = next(iter(model.parameters())).dtype
    inps = torch.ones((args.nsamples, model.seqlen, model.decoder.config.d_model), dtype=dtype, device=dev)
    print('Ready.')
    attention_mask = cache['attention_mask']
    encoder_attention_mask = cache['encoder_attention_mask']
    for i in range(len(layers)):
        layer = layers[i].to(dev)
        full = find_layers(layer)
        sequential = [list(full.keys())]
       
        for names in sequential:
            subset = {n: full[n] for n in names}
            gptq = {}
            for name in subset:
                gptq[name] = GPTQ(subset[name])
                gptq[name].quantizer = Quantizer()
                gptq[name].quantizer.configure(
                    args.wbits, perchannel=True, sym=args.sym, mse=False
                )
                
            def add_batch(name):
                def tmp(_, inp, out):
                    gptq[name].add_batch(inp[0].data, out.data)
                return tmp
            handles = []
            for name in subset:
                handles.append(subset[name].register_forward_hook(add_batch(name)))
            for j in range(args.nsamples):
                outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, 
                                encoder_hidden_states = encoder_hidden_states[j].unsqueeze(0),
                                encoder_attention_mask = encoder_attention_mask)[0]
            for h in handles:
                h.remove()

            for name in subset:
                print(f'Quantizing {name} in layer {i+1}/{len(layers)}...')
                scale,zero,g_idx = gptq[name].fasterquant(percdamp=args.percdamp, groupsize=args.groupsize, actorder=args.act_order)
                quantizers['decoder.block.%d.%s' % (i, name)] = (gptq[name].quantizer.cpu(),scale.cpu(),zero.cpu(),g_idx.cpu())
                gptq[name].free()
                
        for j in range(args.nsamples):
            outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, 
                            encoder_hidden_states = encoder_hidden_states[j].unsqueeze(0),
                            encoder_attention_mask = encoder_attention_mask)[0]

        layers[i] = layer.cpu()
        del layer
        del gptq 
        torch.cuda.empty_cache()

        inps, outs = outs, inps
        
    model.decoder.config.use_cache = use_cache
    model.config.use_cache = use_cache
    return quantizers

# TODO: perform packing on GPU
def t5_pack(model, quantizers, wbits, groupsize):
    layers = find_layers(model)
    layers = {n: layers[n] for n in quantizers}
    make_quant(model, quantizers, wbits, groupsize)
    qlayers = find_layers(model, [QuantLinear])
    print('Packing ...')
    for name in qlayers:
        print(name)
        quantizers[name],scale,zero,g_idx = quantizers[name]
        qlayers[name].pack(layers[name], scale, zero, g_idx)
    print('Done.')
    return model

def load_quant(model, checkpoint, wbits, groupsize=-1):
    from transformers import AutoTokenizer 
    model_max_length = AutoTokenizer.from_pretrained(model, use_fast=False).model_max_length

    from transformers import T5Config, AutoModelForSeq2SeqLM 
    config = T5Config.from_pretrained(model)
    def noop(*args, **kwargs):
        pass
    torch.nn.init.kaiming_uniform_ = noop 
    torch.nn.init.uniform_ = noop 
    torch.nn.init.normal_ = noop 

    torch.set_default_dtype(torch.half)
    transformers.modeling_utils._init_weights = False
    torch.set_default_dtype(torch.half)
    model = AutoModelForSeq2SeqLM.from_config(config)
    torch.set_default_dtype(torch.float)
    model = model.eval()
    layers = find_layers(model)
    for name in ['lm_head']:
        if name in layers:
            del layers[name]
    make_quant(model, layers, wbits, groupsize)

    del layers
    
    print('Loading model ...')
    if checkpoint.endswith('.safetensors'):
        from safetensors.torch import load_file as safe_load
        model.load_state_dict(safe_load(checkpoint), strict = False)
    else:
        model.load_state_dict(torch.load(checkpoint), strict = False)
    model.seqlen = model_max_length
    print('Done.')

    return model

# MMLU
choices = ["A", "B", "C", "D"]

def format_example(df, idx, include_answer=True):
    prompt = df.iloc[idx, 0]
    k = df.shape[1] - 2
    for j in range(k):
        prompt += "\n{}. {}".format(choices[j], df.iloc[idx, j + 1])
    prompt += "\nAnswer:"
    if include_answer:
        prompt += " {}\n\n".format(df.iloc[idx, k + 1])
    return prompt


def gen_prompt(train_df, subject, k=-1):
    def format_subject(subject):
        l = subject.split("_")
        s = ""
        for entry in l:
            s += " " + entry
        return s

    prompt = "The following are multiple choice questions (with answers) about {}.\n\n".format(
        format_subject(subject)
    )
    if k == -1:
        k = train_df.shape[0]
    for i in range(k):
        prompt += format_example(train_df, i)
    return prompt


@torch.no_grad()
def eval(args, subject, model, tokenizer, dev_df, test_df):
    cors = []
    all_probs = []
    answers = choices[: test_df.shape[1] - 2]

    for i in range(test_df.shape[0]):
        print(f'{i + 1}/{test_df.shape[0]}')
        # get prompt and make sure it fits
        k = args.ntrain
        prompt_end = format_example(test_df, i, include_answer=False)
        train_prompt = gen_prompt(dev_df, subject, k)
        prompt = train_prompt + prompt_end

        input_ids = tokenizer(prompt, return_tensors="pt").input_ids.cuda()

        while input_ids.shape[-1] > 2048:
            k -= 1
            train_prompt = gen_prompt(dev_df, subject, k)
            prompt = train_prompt + prompt_end
            input_ids = tokenizer(prompt, return_tensors="pt").input_ids.cuda()

        label = test_df.iloc[i, test_df.shape[1] - 1]

        decoder_input_ids = tokenizer("", return_tensors="pt").input_ids.cuda()
        decoder_input_ids = model._shift_right(decoder_input_ids)
        logits = model(
            input_ids=input_ids, decoder_input_ids=decoder_input_ids
        ).logits.flatten().float()

        probs = (
            torch.nn.functional.softmax(
                torch.tensor(
                    [
                        logits[tokenizer("A").input_ids[0]],
                        logits[tokenizer("B").input_ids[0]],
                        logits[tokenizer("C").input_ids[0]],
                        logits[tokenizer("D").input_ids[0]],
                    ]
                ),
                dim=0,
            )
            .detach()
            .cpu()
            .numpy()
        )
        pred = {0: "A", 1: "B", 2: "C", 3: "D"}[np.argmax(probs)]

        cor = pred == label
        cors.append(cor)
        all_probs.append(probs)

    acc = np.mean(cors)
    cors = np.array(cors)

    all_probs = np.array(all_probs)
    print("Average accuracy {:.3f} - {}".format(acc, subject))

    return cors, acc, all_probs


def benchmark(model, tokenizer, args):
    heads_per_gpu = len(model.encoder.block) // args.ngpu
    device_map = {
        gpu: list(
            range(
                0 + (gpu * heads_per_gpu),
                (0 + (gpu * heads_per_gpu)) + heads_per_gpu,
            )
        )
        for gpu in range(args.ngpu)
    }
    model.parallelize(device_map)
    subjects = sorted(
        ['college_biology']
    )

    all_cors = []
    for subject in subjects:
        dev_df = pd.read_csv(
            os.path.join(args.data_dir, "dev", subject + "_dev.csv"), header=None
        )[: args.ntrain]
        test_df = pd.read_csv(
            os.path.join(args.data_dir, "test", subject + "_test.csv"), header=None
        )

        cors, acc, probs = eval(args, subject, model, tokenizer, dev_df, test_df)
        
if __name__ == '__main__':
    import argparse
    from datautils import *

    parser = argparse.ArgumentParser()

    parser.add_argument(
        'model', type=str,
        help='t5 model to load'
    )
    parser.add_argument(
        'dataset', type=str, choices=['wikitext2', 'ptb', 'c4'],
        help='Where to extract calibration data from.'
    )
    parser.add_argument(
        '--seed',
        type=int, default=0, help='Seed for sampling the calibration data.'
    )
    parser.add_argument(
        '--nsamples', type=int, default=128,
        help='Number of calibration data samples.'
    )
    parser.add_argument(
        '--percdamp', type=float, default=.01,
        help='Percent of the average Hessian diagonal to use for dampening.'
    )
    parser.add_argument(
        '--nearest', action='store_true',
        help='Whether to run the RTN baseline.'
    ) 
    parser.add_argument(
        '--wbits', type=int, default=16, choices=[2, 3, 4, 8, 16],
        help='#bits to use for quantization; use 16 for evaluating base model.'
    )
    parser.add_argument(
        '--trits', action='store_true',
        help='Whether to use trits for quantization.'
    )
    parser.add_argument(
        '--groupsize', type=int, default=-1,
        help='Groupsize to use for quantization; default uses full row.'
    )
    parser.add_argument(
        '--save', type=str, default='',
        help='Save quantized checkpoint under this name.'
    )
    parser.add_argument(
        '--save_safetensors', type=str, default='',
        help='Save quantized `.safetensors` checkpoint under this name.'
    )
    parser.add_argument(
        '--load', type=str, default='',
        help='Load quantized model.'
    )
    parser.add_argument(
        '--sym', action='store_true',
        help='Whether to perform symmetric quantization.'
    )
    parser.add_argument(
        '--act-order', action='store_true',
        help='Whether to apply the activation order GPTQ heuristic'
    )
    parser.add_argument(
        '--benchmark', action='store_true',
        help='MMLU benchmarking'
    )
    parser.add_argument(
        '--ntrain', "-k", type=int, default=5,
        help='Number of k-shot to use for MMLU benchmarking.'
    )
    parser.add_argument(
        "--ngpu", "-g", type=int, default=1,
        help='Number of gpu to use for MMLU benchmarking.'
    )
    parser.add_argument(
        "--data_dir", "-d", type=str, default="data",
        help='MMLU dataset path'
    )
    
    args = parser.parse_args()

    if type(args.load) is not str:
        args.load = args.load.as_posix()
    
    if args.load:
        model = load_quant(args.model, args.load, args.wbits, args.groupsize)
    else:
        model = get_t5(args.model)
        model.eval()
        
    if not args.load:
        dataloader, testloader = get_loaders(
            args.dataset, nsamples=args.nsamples, seed=args.seed, model=args.model, seqlen=model.seqlen
        )

    if not args.load and args.wbits < 16 and not args.nearest:
        tick = time.time()
        quantizers = t5_sequential(model, dataloader, DEV)
        print(time.time() - tick)
    
    if args.load and args.benchmark:
        model = model.to(DEV)
        from transformers import T5Tokenizer
        tokenizer = T5Tokenizer.from_pretrained(args.model)

        benchmark(model, tokenizer, args)
        
    if args.load:
        exit()

    if args.save:
        t5_pack(model, quantizers, args.wbits, args.groupsize)
        torch.save(model.state_dict(), args.save) 

    if args.save_safetensors:
        t5_pack(model, quantizers, args.wbits, args.groupsize)
        from safetensors.torch import save_file as safe_save
        safe_save(model.state_dict(), args.save_safetensors)
