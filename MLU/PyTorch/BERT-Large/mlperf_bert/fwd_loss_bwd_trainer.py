
import torch
from function import graph
#from apex import amp

class FwdLossBwdTrainer():

    def __init__(self, args, grad_scaler):
        super(FwdLossBwdTrainer, self).__init__()
        self.args = args
        self.grad_scaler = grad_scaler
        if args.use_mlu:
            import torch_mlu.core.mlu_model as ct
            self.capture_stream = ct.current_queue()
        else:
            self.capture_stream = torch.cuda.Stream()

        self.send_stats_in_parallel = False
        if args.use_mlu:
            import torch_mlu.core.mlu_model as ct
            self.stats_stream = ct.current_queue()
        else:
            self.stats_stream = torch.cuda.Stream()
        self.loss_cpu = torch.tensor(0.0, dtype=torch.float32, device='cpu').pin_memory()
        self.mlm_acc_cpu = torch.tensor(0.0, dtype=torch.float32, device='cpu').pin_memory()

    def capture_bert_model_segment_graph(self, bert_model, use_cuda_graph):
        # eval batch depends on the rank, since eval sample count isn't divisible by world size
        rank = torch.distributed.get_rank()
        world_size = torch.distributed.get_world_size()
        eval_batch_min = self.args.num_eval_examples // world_size
        remainder = self.args.num_eval_examples % world_size
        if rank<remainder:
            eval_batch = eval_batch_min + 1
        else:
            eval_batch = eval_batch_min
        eval_batch = min(eval_batch, self.args.eval_batch_size)
        batches_to_graph = [eval_batch, self.args.train_batch_size]

        bert_model_segment = bert_model.bert_model_segment
        sample_model_train = [
                 torch.ones(self.args.train_batch_size, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(self.args.train_batch_size, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(self.args.train_batch_size, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 ]
        sample_model_eval = [
                 torch.ones(eval_batch, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(eval_batch, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 torch.ones(eval_batch, self.args.max_seq_length, dtype=torch.int64, device=self.args.device),
                 ]
        bert_model_segment = graph(bert_model_segment,
                                    tuple(t.clone() for t in sample_model_train),
                                    tuple(t.clone() for t in sample_model_eval) if self.args.eval_batch_size * world_size >= self.args.num_eval_examples else None,
                                    self.capture_stream,
                                    warmup_iters=8,
                                    warmup_only=(not use_cuda_graph),
                                    use_mlu=self.args.use_mlu)

        bert_head_segment = bert_model.heads_only_segment
        sample_head_train = [
                torch.ones(self.args.train_batch_size, self.args.max_seq_length, 1024, dtype=torch.float16 if self.args.fp16 else torch.float32, device=self.args.device),
                torch.ones(self.args.train_batch_size,                           1024, dtype=torch.float16 if self.args.fp16 else torch.float32, device=self.args.device),
                torch.ones(self.args.train_batch_size, self.args.max_seq_length,       dtype=torch.int64, device=self.args.device),
                torch.ones(self.args.train_batch_size,                                 dtype=torch.int64, device=self.args.device),
                ]
        sample_head_eval = [
                torch.ones(eval_batch, self.args.max_seq_length, 1024, dtype=torch.float16 if self.args.fp16 else torch.float32, device=self.args.device),
                torch.ones(eval_batch,                           1024, dtype=torch.float16 if self.args.fp16 else torch.float32, device=self.args.device),
                torch.ones(eval_batch, self.args.max_seq_length,       dtype=torch.int64, device=self.args.device),
                torch.ones(eval_batch,                                 dtype=torch.int64, device=self.args.device),
                ]
        sample_head_tuple_train = tuple([sample_head_train[0].clone().requires_grad_(), sample_head_train[1].clone().requires_grad_(), sample_head_train[2].clone(), sample_head_train[3].clone()])
        sample_head_tuple_eval = tuple([sample_head_eval[0].clone(), sample_head_eval[1].clone(), sample_head_eval[2].clone(), sample_head_eval[3].clone()])
        bert_head_segment = graph(bert_head_segment,
                                               sample_head_tuple_train,
                                               sample_head_tuple_eval if self.args.eval_batch_size * world_size >= self.args.num_eval_examples else None,
                                               self.capture_stream,
                                               warmup_iters=8,
                                               warmup_only=(not use_cuda_graph),
                                               use_mlu=self.args.use_mlu)


        return bert_model

    def eval_step(self, batch, model):
        model.eval()
        input_ids, segment_ids, input_mask, masked_lm_labels, next_sentence_labels = batch
        loss = None
        mlm_acc = None

        loss, mlm_acc, num_valid = model(input_ids, segment_ids, input_mask,
                        masked_lm_labels, next_sentence_labels)
        return loss, mlm_acc, num_valid

    def step(self, step, batch, model, optimizer):
        input_ids, segment_ids, input_mask, masked_lm_labels, next_sentence_labels = batch
        loss = None
        mlm_acc = None

        loss, mlm_acc, _ = model(input_ids, segment_ids, input_mask,
                        masked_lm_labels, next_sentence_labels)

        if self.send_stats_in_parallel:
            self.stats_stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(self.stats_stream):
                self.loss_cpu.copy_(loss.detach(), non_blocking=True)
                self.mlm_acc_cpu.copy_(mlm_acc.detach(), non_blocking=True)

        if self.args.bypass_amp:
            loss.backward()
        elif self.args.distributed_lamb:
            optimizer._lazy_init_stage1()
            self.grad_scaler.scale(loss).backward()
            optimizer._lazy_init_stage2()
        else:
            if self.args.use_mlu:
                import cnmix as amp
            else:
                from apex import amp

            with amp.scale_loss(loss, optimizer, delay_overflow_check=self.args.allreduce_post_accumulation) as scaled_loss:
                scaled_loss.backward()

        if self.send_stats_in_parallel:
            self.stats_stream.synchronize()
            loss = self.loss_cpu
            mlm_acc = self.mlm_acc_cpu

        return loss, mlm_acc
