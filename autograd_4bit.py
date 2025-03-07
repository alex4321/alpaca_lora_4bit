import logging

import torch
import torch.nn as nn
import time
import math
from torch.cuda.amp import custom_bwd, custom_fwd
from colorama import init, Fore, Back, Style
from huggingface_hub.utils._validators import HFValidationError
init(autoreset=True)


gptq_backend_loaded = False
triton_backend_loaded = False


class AutogradMatmul4bitNotImplemented(torch.autograd.Function):
    @staticmethod
    @custom_fwd(cast_inputs=torch.float16)
    def forward(ctx, x, qweight, scales, zeros, g_idx, bits, maxq):
        raise NotImplementedError()

    @staticmethod
    @custom_bwd
    def backward(ctx, grad_output):
        raise NotImplementedError()


try:
    import matmul_utils_4bit as mm4b

    class AutogradMatmul4bitCuda(torch.autograd.Function):

        @staticmethod
        @custom_fwd(cast_inputs=torch.float16)
        def forward(ctx, x, qweight, scales, zeros, g_idx, bits, maxq):
            ctx.save_for_backward(qweight, scales, zeros, g_idx)
            if g_idx is None:
                output = mm4b._matmul4bit_v1_recons(x, qweight, scales, zeros)
            else:
                output = mm4b._matmul4bit_v2_recons(x, qweight, scales, zeros, g_idx)
            output = output.clone()
            return output

        @staticmethod
        @custom_bwd
        def backward(ctx, grad_output):
            qweight, scales, zeros, g_idx = ctx.saved_tensors
            if ctx.needs_input_grad[0]:
                if g_idx is None:
                    grad = mm4b._matmul4bit_v1_recons(grad_output, qweight, scales, zeros, transpose=True)
                else:
                    grad = mm4b._matmul4bit_v2_recons(grad_output, qweight, scales, zeros, g_idx, transpose=True)
            return grad, None, None, None, None, None, None

    class AutogradMatmul2bitCuda(torch.autograd.Function):

        @staticmethod
        @custom_fwd(cast_inputs=torch.float16)
        def forward(ctx, x, qweight, scales, zeros, g_idx, bits, maxq):
            ctx.save_for_backward(qweight, scales, zeros, g_idx)
            output = mm4b._matmul2bit_v2_recons(x, qweight, scales, zeros, g_idx)
            output = output.clone()
            return output

        @staticmethod
        @custom_bwd
        def backward(ctx, grad_output):
            qweight, scales, zeros, g_idx = ctx.saved_tensors
            if ctx.needs_input_grad[0]:
                grad = mm4b._matmul2bit_v2_recons(grad_output, qweight, scales, zeros, g_idx, transpose=True)
            return grad, None, None, None, None, None, None

    gptq_backend_loaded = True
except ImportError:
    print('quant_cuda not found. Please run "pip install alpaca_lora_4bit[cuda]".')


try:
    import triton_utils as tu


    class AutogradMatmul4bitTriton(torch.autograd.Function):

        @staticmethod
        @custom_fwd(cast_inputs=torch.float16)
        def forward(ctx, x, qweight, scales, qzeros, g_idx, bits, maxq):
            output = tu.triton_matmul(x, qweight, scales, qzeros, g_idx, bits, maxq)
            ctx.save_for_backward(qweight, scales, qzeros, g_idx)
            ctx.bits, ctx.maxq = bits, maxq
            output = output.clone()
            return output

        @staticmethod
        @custom_bwd
        def backward(ctx, grad_output):
            qweight, scales, qzeros, g_idx = ctx.saved_tensors
            bits, maxq = ctx.bits, ctx.maxq
            grad_input = None

            if ctx.needs_input_grad[0]:
                grad_input = tu.triton_matmul_transpose(grad_output, qweight, scales, qzeros, g_idx, bits, maxq)
            return grad_input, None, None, None, None, None, None


    triton_backend_loaded = True
except ImportError:
    print('Triton not found. Please run "pip install triton".')


def is_triton_backend_available():
    return 'AutogradMatmul4bitTriton' in globals()


def is_gptq_backend_available():
    return 'AutogradMatmul4bitCuda' in globals()


AutogradMatmul4bit = AutogradMatmul4bitNotImplemented
AutogradMatmul2bit = AutogradMatmul4bitNotImplemented
backend = None
if is_gptq_backend_available():
    AutogradMatmul4bit = AutogradMatmul4bitCuda
    AutogradMatmul2bit = AutogradMatmul2bitCuda
    backend = 'cuda'
elif is_triton_backend_available():
    AutogradMatmul4bit = AutogradMatmul4bitTriton
    backend = 'triton'
else:
    logging.warning("Neither gptq/cuda or triton backends are available.")


def switch_backend_to(to_backend):
    global AutogradMatmul4bit
    global backend
    if to_backend == 'cuda':
        if not is_gptq_backend_available():
            raise ValueError('quant_cuda not found. Please reinstall with pip install .')
        AutogradMatmul4bit = AutogradMatmul4bitCuda
        backend = 'cuda'
        print(Style.BRIGHT + Fore.GREEN + 'Using CUDA implementation.')
    elif to_backend == 'triton':
        # detect if AutogradMatmul4bitTriton is defined
        if not is_triton_backend_available():
            raise ValueError('Triton not found. Please install triton')
        AutogradMatmul4bit = AutogradMatmul4bitTriton
        backend = 'triton'
        print(Style.BRIGHT + Fore.GREEN + 'Using Triton implementation.')
    else:
        raise ValueError('Backend not supported.')


def matmul4bit_with_backend(x, qweight, scales, qzeros, g_idx, bits, maxq, groupsize=None):
    if backend == 'cuda':
        return mm4b.matmul4bit(x, qweight, scales, qzeros, g_idx, groupsize)
    elif backend == 'triton':
        assert qzeros.dtype == torch.int32
        return tu.triton_matmul(x, qweight, scales, qzeros, g_idx, bits, maxq)
    else:
        raise ValueError('Backend not supported.')


# Assumes layer is perfectly divisible into 256 * 256 blocks
class Autograd4bitQuantLinear(nn.Module):

    def __init__(self, in_features, out_features, groupsize=-1, is_v1_model=False, bits=4):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.bits = bits
        self.maxq = 2 ** self.bits - 1
        groupsize = groupsize if groupsize != -1 else in_features
        self.groupsize = groupsize
        self.is_v1_model = is_v1_model
        self.disable_bias = False
        if is_v1_model:
            self.register_buffer('zeros', torch.empty((out_features, 1)))
            self.register_buffer('scales', torch.empty((out_features, 1)))
            self.g_idx = None
        else:
            self.register_buffer('qzeros',
                                  torch.empty((math.ceil(in_features/groupsize), out_features // 256 * (bits * 8)), dtype=torch.int32)
                                )
            self.register_buffer('scales', torch.empty((math.ceil(in_features/groupsize), out_features)))
            self.register_buffer('g_idx', torch.tensor([i // self.groupsize  for i in range(in_features)], dtype = torch.int32))
        self.register_buffer('bias', torch.empty(out_features))
        self.register_buffer(
            'qweight', torch.empty((in_features // 256 * (bits * 8), out_features), dtype=torch.int32)
        )


    def forward(self, x):
        if self.bits == 4:
            if torch.is_grad_enabled():
                out = AutogradMatmul4bit.apply(x, self.qweight, self.scales,
                                            self.qzeros if not self.is_v1_model else self.zeros,
                                            self.g_idx, self.bits, self.maxq)
            else:
                out = matmul4bit_with_backend(x, self.qweight, self.scales,
                                            self.qzeros if not self.is_v1_model else self.zeros,
                                            self.g_idx, self.bits, self.maxq, self.groupsize)
        elif self.bits == 2:
            out = AutogradMatmul2bit.apply(x, self.qweight, self.scales, self.qzeros, self.g_idx, self.bits, self.maxq)
        else:
            raise ValueError('Unsupported bitwidth.')
        if not self.disable_bias:
            out += self.bias
        return out


def make_quant_for_4bit_autograd(module, names, name='', groupsize=-1, is_v1_model=False, bits=4):
    if isinstance(module, Autograd4bitQuantLinear):
        return
    for attr in dir(module):
        tmp = getattr(module, attr)
        name1 = name + '.' + attr if name != '' else attr
        if name1 in names:
            setattr(
                module, attr, Autograd4bitQuantLinear(tmp.in_features, tmp.out_features, groupsize=groupsize, is_v1_model=is_v1_model, bits=bits)
            )
    for name1, child in module.named_children():
        make_quant_for_4bit_autograd(child, names, name + '.' + name1 if name != '' else name1, groupsize=groupsize, is_v1_model=is_v1_model, bits=bits)


def model_to_half(model):
    model.half()
    for n, m in model.named_modules():
        if isinstance(m, Autograd4bitQuantLinear):
            if m.is_v1_model:
                m.zeros = m.zeros.half()
            m.scales = m.scales.half()
            m.bias = m.bias.half()
    print(Style.BRIGHT + Fore.YELLOW + 'Converted as Half.')


def model_to_float(model):
    model.float()
    for n, m in model.named_modules():
        if isinstance(m, Autograd4bitQuantLinear):
            if m.is_v1_model:
                m.zeros = m.zeros.float()
            m.scales = m.scales.float()
            m.bias = m.bias.float()
    print(Style.BRIGHT + Fore.YELLOW + 'Converted as Float.')


def find_layers(module, layers=[nn.Conv2d, nn.Linear], name=''):
    if type(module) in layers:
        return {name: module}
    res = {}
    for name1, child in module.named_children():
        res.update(find_layers(
            child, layers=layers, name=name + '.' + name1 if name != '' else name1
        ))
    return res


def load_llama_model_4bit_low_ram(config_path, model_path, groupsize=-1, half=False, device_map="auto", seqlen=2048, is_v1_model=False, bits=4):
    import accelerate
    from transformers import LlamaConfig, LlamaForCausalLM, LlamaTokenizer

    print(Style.BRIGHT + Fore.CYAN + "Loading Model ...")
    t0 = time.time()

    with accelerate.init_empty_weights():
        config = LlamaConfig.from_pretrained(config_path)
        model = LlamaForCausalLM(config)
        model = model.eval()
        layers = find_layers(model)
        for name in ['lm_head']:
            if name in layers:
                del layers[name]
        make_quant_for_4bit_autograd(model, layers, groupsize=groupsize, is_v1_model=is_v1_model, bits=bits)
    model = accelerate.load_checkpoint_and_dispatch(
        model=model,
        checkpoint=model_path,
        device_map=device_map,
        no_split_module_classes=["LlamaDecoderLayer"]
    )

    model.seqlen = seqlen

    if half:
        model_to_half(model)

    try:
        tokenizer = LlamaTokenizer.from_pretrained(config_path)
    except HFValidationError as e:
        tokenizer = LlamaTokenizer.from_pretrained(model)
    tokenizer.truncation_side = 'left'

    print(Style.BRIGHT + Fore.GREEN + f"Loaded the model in {(time.time()-t0):.2f} seconds.")

    return model, tokenizer

def load_llama_model_4bit_low_ram_and_offload(config_path, model_path, lora_path=None, groupsize=-1, seqlen=2048, max_memory=None, is_v1_model=False, bits=4):
    import accelerate
    from transformers import LlamaConfig, LlamaForCausalLM, LlamaTokenizer

    if max_memory is None:
        max_memory = {0: '24Gib', 'cpu': '48Gib'}

    print(Style.BRIGHT + Fore.CYAN + "Loading Model ...")
    t0 = time.time()

    with accelerate.init_empty_weights():
        config = LlamaConfig.from_pretrained(config_path)
        model = LlamaForCausalLM(config)
        model = model.eval()
        layers = find_layers(model)
        for name in ['lm_head']:
            if name in layers:
                del layers[name]
        make_quant_for_4bit_autograd(model, layers, groupsize=groupsize, is_v1_model=is_v1_model, bits=bits)
    accelerate.load_checkpoint_in_model(model, checkpoint=model_path, device_map={'': 'cpu'})

    # rotary_emb fix
    for n, m in model.named_modules():
        if 'rotary_emb' in n:
            cos_cached = m.cos_cached.clone().cpu()
            sin_cached = m.sin_cached.clone().cpu()
            break

    if lora_path is not None:
        # Apply Monkey Patch
        from monkeypatch.peft_tuners_lora_monkey_patch import replace_peft_model_with_gptq_lora_model
        replace_peft_model_with_gptq_lora_model()
        from peft import PeftModel
        from monkeypatch.peft_tuners_lora_monkey_patch import Linear4bitLt
        model = PeftModel.from_pretrained(model, lora_path, device_map={'': 'cpu'}, torch_dtype=torch.float32, is_trainable=True)
        print(Style.BRIGHT + Fore.GREEN + '{} Lora Applied.'.format(lora_path))

    model.seqlen = seqlen

    print('Apply half ...')
    for n, m in model.named_modules():
        if isinstance(m, Autograd4bitQuantLinear) or ((lora_path is not None) and isinstance(m, Linear4bitLt)):
            if m.is_v1_model:
                m.zeros = m.zeros.half()
            m.scales = m.scales.half()
            m.bias = m.bias.half()

    print('Dispatching model ...')
    device_map = accelerate.infer_auto_device_map(model, max_memory=max_memory, no_split_module_classes=["LlamaDecoderLayer"])
    model = accelerate.dispatch_model(model, device_map=device_map, offload_buffers=True, main_device=0)
    torch.cuda.empty_cache()
    print(Style.BRIGHT + Fore.YELLOW + 'Total {:.2f} Gib VRAM used.'.format(torch.cuda.memory_allocated() / 1024 / 1024))

    # rotary_emb fix
    for n, m in model.named_modules():
        if 'rotary_emb' in n:
            if getattr(m, '_hf_hook', None):
                if isinstance(m._hf_hook, accelerate.hooks.SequentialHook):
                    hooks = m._hf_hook.hooks
                else:
                    hooks = [m._hf_hook]
                for hook in hooks:
                    if hook.offload:
                        if n + '.sin_cached' not in hook.weights_map.dataset.state_dict.keys():
                            hook.weights_map.dataset.state_dict[n + '.sin_cached'] = sin_cached.clone().cpu()
                            hook.weights_map.dataset.state_dict[n + '.cos_cached'] = cos_cached.clone().cpu()

    tokenizer = LlamaTokenizer.from_pretrained(config_path)
    tokenizer.truncation_side = 'left'

    print(Style.BRIGHT + Fore.GREEN + f"Loaded the model in {(time.time()-t0):.2f} seconds.")

    return model, tokenizer

load_llama_model_4bit_low_ram_and_offload_to_cpu = load_llama_model_4bit_low_ram_and_offload
